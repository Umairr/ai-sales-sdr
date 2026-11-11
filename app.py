"""
AI Sales SDR - multi-agent cold outreach with an autonomous reply-handling SDR.

Pipeline
1. Orchestration in code: three sales agents with different styles draft emails in
   parallel, a picker selects the best one, and a critic/refiner loop improves it
   (Python decides when to stop).
2. Orchestration by an LLM: a Sales Manager agent uses the writers, critic and refiner
   as tools, then hands off to an Email Manager that writes the subject, converts the
   body to HTML and sends it.
3. Live conversation: emails go out through Resend (with SMTP fallback), prospect
   replies are read from the inbox over IMAP, and an SDR agent answers each reply in
   the same thread to keep the conversation moving.
4. Web UI: pick or write an objective, choose an orchestration mode, watch every agent
   step, preview the HTML email, and continue the conversation with a reply from your
   inbox, an AI-simulated prospect, or text you type.

Configuration (.env): see .env.example

Run:
    uv run app.py                            # Gradio web app (default)
    uv run app.py --mode code                # code orchestration, CLI
    uv run app.py --mode llm                 # LLM orchestration, CLI
    uv run app.py --mode full                # send + wait for real replies
    uv run app.py --mode full --simulate     # an AI plays the prospect
"""

import argparse
import asyncio
import email
import email.header
import email.utils
import imaplib
import os
import smtplib
import time
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr

import requests
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from agents import (
    Agent,
    ModelSettings,
    OpenAIChatCompletionsModel,
    Runner,
    function_tool,
    set_default_openai_api,
    set_default_openai_client,
    set_tracing_disabled,
    trace,
)
from openai import AsyncOpenAI

load_dotenv(override=True)

# ---------------------------------------------------------------------------
# LLM provider: OpenAI, OpenRouter (incl. Claude, Gemini, Llama...), Grok, Gemini, Ollama
# ---------------------------------------------------------------------------
# name: (base_url, key env vars, default model)
PROVIDERS = {
    "openai": (None, ("OPENAI_API_KEY",), "gpt-5.4-mini"),
    "openrouter": ("https://openrouter.ai/api/v1", ("OPENROUTER_API_KEY",), "openai/gpt-5.4-mini"),
    "grok": ("https://api.x.ai/v1", ("XAI_API_KEY", "GROK_API_KEY"), "grok-4.7"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "gemini-2.5-flash"),
    "ollama": (os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"), (), "llama3.2"),
}
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").lower()
if LLM_PROVIDER not in PROVIDERS:
    raise SystemExit(f"Unknown LLM_PROVIDER '{LLM_PROVIDER}'. Choose one of: {', '.join(PROVIDERS)}")
_base_url, _key_envs, _default_model = PROVIDERS[LLM_PROVIDER]
MODEL_NAME = os.getenv("LLM_MODEL") or _default_model

if LLM_PROVIDER != "openai":
    _api_key = next((os.getenv(k) for k in _key_envs if os.getenv(k)), None) or ("ollama" if not _key_envs else None)
    if not _api_key:
        raise SystemExit(f"LLM_PROVIDER={LLM_PROVIDER} needs one of: {', '.join(_key_envs)}")
    _client = AsyncOpenAI(base_url=_base_url, api_key=_api_key)
    set_default_openai_client(_client, use_for_tracing=False)
    set_default_openai_api("chat_completions")  # other providers speak Chat Completions, not Responses
    # Wrap the model so ids like "anthropic/claude-..." go to the provider as-is
    MODEL = OpenAIChatCompletionsModel(model=MODEL_NAME, openai_client=_client)
else:
    MODEL = MODEL_NAME
    if not os.getenv("OPENAI_API_KEY"):
        set_tracing_disabled(True)  # traces are uploaded to OpenAI; skip when there is no OpenAI key


def llm_settings(**kwargs) -> ModelSettings:
    """Per-agent model settings that work on every provider."""
    if LLM_PROVIDER != "openai":
        # OpenRouter & co. reserve the model's whole output window when max_tokens is unset
        kwargs.setdefault("max_tokens", int(os.getenv("LLM_MAX_TOKENS", "4096")))
        # Claude Opus 5.5 / Sonnet 5.5 / Fable reject forced tool choice; the instructions already demand the tools
        if kwargs.get("tool_choice") == "required" and any(
            tag in MODEL_NAME.lower() for tag in ("opus-5.5", "opus-5-5", "sonnet-5.5", "sonnet-5-5", "fable", "mythos")
        ):
            kwargs.pop("tool_choice")
    return ModelSettings(**kwargs)


print(f"🤖 Using {LLM_PROVIDER}:{MODEL_NAME}")

RESEND_API_KEY = os.getenv("RESEND_API_KEY")
RESEND_FROM = os.getenv("RESEND_FROM", "ComplAI <onboarding@resend.dev>")
EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS")
EMAIL_SMTP_SERVER = os.getenv("EMAIL_SMTP_SERVER")
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD")
PROSPECT_EMAIL = os.getenv("PROSPECT_EMAIL", EMAIL_ADDRESS)
IMAP_SERVER = os.getenv("IMAP_SERVER") or (EMAIL_SMTP_SERVER or "").replace("smtp", "imap", 1)

MAX_REFINE_ROUNDS = 3
MAX_CONVERSATION_TURNS = 3
POLL_SECONDS = 30
REPLY_TIMEOUT_SECONDS = 15 * 60


# ---------------------------------------------------------------------------
# Email provider: Resend (preferred) with SMTP fallback
# ---------------------------------------------------------------------------

# Keeps the email thread so replies show up as one conversation
thread = {"subject": None, "message_ids": [], "history": [], "processed": set(), "last_sent_at": 0.0}

# The UI can turn real sending off (dry run); the last email is kept so the UI can show it
settings = {"send_enabled": True}
last_email = {}


def send_email(subject: str, text_body: str, html_body: str, to: str = PROSPECT_EMAIL) -> str:
    message_id = make_msgid(domain="complai.local")
    headers = {"Message-ID": message_id}
    if thread["message_ids"]:
        headers["In-Reply-To"] = thread["message_ids"][-1]
        headers["References"] = " ".join(thread["message_ids"])

    last_email.update(subject=subject, text_body=text_body, html_body=html_body, to=to)

    if not settings["send_enabled"]:
        provider = "dry run (not sent)"
    elif RESEND_API_KEY:
        payload = {
            "from": RESEND_FROM,
            "to": [to],
            "subject": subject,
            "text": text_body,
            "html": html_body,
            "headers": headers,
        }
        if EMAIL_ADDRESS:
            payload["reply_to"] = EMAIL_ADDRESS
        response = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        provider = f"Resend (id={response.json().get('id')})"
    else:
        msg = EmailMessage()
        msg["From"] = EMAIL_ADDRESS
        msg["To"] = to
        msg["Subject"] = subject
        for key, value in headers.items():
            msg[key] = value
        msg.set_content(text_body)
        msg.add_alternative(html_body, subtype="html")
        with smtplib.SMTP(EMAIL_SMTP_SERVER, 587) as server:
            server.starttls()
            server.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
            server.send_message(msg)
        provider = "SMTP"

    thread["subject"] = thread["subject"] or subject
    thread["message_ids"].append(message_id)
    thread["last_sent_at"] = time.time() - 60
    thread["history"].append(f"SDR (us):\n{text_body}")
    last_email["provider"] = provider
    print(f"\n📧 Sent via {provider} to {to}: {subject}")
    return message_id


@function_tool
def send_email_tool(subject: str, text_body: str, html_body: str) -> str:
    """
    Send the email to the sales prospect.

    Args:
        subject: The subject of the email
        text_body: The body of the email as plain text
        html_body: The body of the email as HTML
    """
    send_email(subject, text_body, html_body)
    return "Email sent successfully"


# ---------------------------------------------------------------------------
# Reading replies (IMAP)
# ---------------------------------------------------------------------------

def _plain_text(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
        return ""
    return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")


def _strip_quoted(text: str) -> str:
    """Keep only the new part of a reply, drop the quoted previous email."""
    lines = []
    for line in text.splitlines():
        if line.startswith(">") or (line.startswith("On ") and "wrote:" in line):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _is_reply(msg) -> bool:
    """
    Resend (Amazon SES) replaces our Message-ID, so we can't rely on In-Reply-To alone.
    A reply either references a known message of the thread, or is "Re: <our subject>".
    """
    refs = f"{msg.get('In-Reply-To', '')} {msg.get('References', '')}"
    if any(mid in refs for mid in thread["message_ids"]):
        return True
    subject = str(email.header.make_header(email.header.decode_header(msg.get("Subject", ""))))
    return subject.lower().lstrip().startswith("re:") and thread["subject"].lower() in subject.lower()


def wait_for_reply(timeout: int = REPLY_TIMEOUT_SECONDS) -> str | None:
    """Poll the inbox for a new reply to our thread (seen or unseen)."""
    print(f"\n⏳ Waiting for a reply in {EMAIL_ADDRESS} (timeout {timeout // 60} min)...")
    our_sender = parseaddr(RESEND_FROM)[1].lower() if RESEND_API_KEY else None
    since = time.strftime("%d-%b-%Y", time.gmtime(time.time() - 86400))
    deadline = time.time() + timeout
    while time.time() < deadline:
        with imaplib.IMAP4_SSL(IMAP_SERVER) as imap:
            imap.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
            imap.select("INBOX", readonly=True)
            # Not UNSEEN: replying from the same Gmail account marks the reply as already read
            _, data = imap.search(None, "SINCE", since)
            for num in data[0].split():
                _, msg_data = imap.fetch(num, "(BODY.PEEK[])")
                msg = email.message_from_bytes(msg_data[0][1])
                message_id = msg.get("Message-ID", "").strip()
                sender = parseaddr(msg.get("From"))[1].lower()
                if message_id in thread["processed"] or sender == our_sender or not _is_reply(msg):
                    continue
                if email.utils.parsedate_to_datetime(msg["Date"]).timestamp() < thread["last_sent_at"]:
                    continue
                thread["processed"].add(message_id)
                thread["message_ids"].append(message_id)  # our next email threads under this reply
                body = _strip_quoted(_plain_text(msg))
                print(f"\n📨 Reply from {sender}:\n{body}")
                return body
        time.sleep(POLL_SECONDS)
    print("No reply received.")
    return None


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

intro = """
You are a sales agent working for ComplAI,
a company that provides a SaaS tool for ensuring SOC2 compliance and preparing for audits, powered by AI.
You write emails.
Never use placeholders like [First Name], [Company Name] or [Your Name] - the email is sent as-is.
Greet with "Hi there," and sign off as "Alex, ComplAI".
"""

sales_agent1 = Agent(name="Professional Sales Agent", model=MODEL, model_settings=llm_settings(),
                     instructions=intro + "Your email style is professional, serious, with gravitas and credibility.")
sales_agent2 = Agent(name="Humorous Sales Agent", model=MODEL, model_settings=llm_settings(),
                     instructions=intro + "Your email style is witty, engaging, and humorous.")
sales_agent3 = Agent(name="Executive Sales Agent", model=MODEL, model_settings=llm_settings(),
                     instructions=intro + "Your email style is concise, to the point, in the style of a busy senior executive.")

sales_picker = Agent(
    name="Sales Picker",
    model=MODEL, model_settings=llm_settings(),
    instructions="""
You pick the best cold sales email from the given options.
Imagine you are a customer and pick the one you are most likely to respond to.
Do not give an explanation; reply with the selected email only.
""",
)


class EmailFeedback(BaseModel):
    approved: bool = Field(description="True if the email is ready to send as-is")
    score: int = Field(description="Quality score from 1 to 10")
    feedback: str = Field(description="Concrete, actionable suggestions to improve the email")


email_critic = Agent(
    name="Email Critic",
    model=MODEL, model_settings=llm_settings(),
    output_type=EmailFeedback,
    instructions="""
You are a demanding head of sales reviewing a cold email before it goes out.
Judge it on: a compelling subject line, personalisation, clarity of value, brevity
(under 150 words), and a single clear, low-friction call to action.
Never approve an email that contains placeholders in square brackets like [First Name].
Only approve if the score is 8 or higher.
""",
)

email_refiner = Agent(
    name="Email Refiner",
    model=MODEL, model_settings=llm_settings(),
    instructions=intro + """
You are given a draft cold email and feedback on it.
Rewrite the email applying the feedback. Keep what already works.
Reply with the improved email only (subject line first).
""",
)

subject_writer = Agent(name="Subject Writer", model=MODEL, model_settings=llm_settings(),
                       instructions="Write a subject line for a cold sales email that is likely to get a response. "
                                    "Never use placeholders in square brackets like [goal] or [Company]. Reply with the subject only.")
html_converter = Agent(name="HTML Converter", model=MODEL, model_settings=llm_settings(),
                       instructions="Convert a text email body to a simple, clean, well-laid-out HTML email body. Reply with the HTML only.")

email_manager = Agent(
    name="Email Manager",
    model=MODEL,
    tools=[
        subject_writer.as_tool(tool_name="subject_writer", tool_description="Write a subject for a cold sales email"),
        html_converter.as_tool(tool_name="html_converter", tool_description="Convert a text email body to an HTML email body"),
        send_email_tool,
    ],
    handoff_description="Format the chosen email to HTML and send it",
    model_settings=llm_settings(tool_choice="required"),
    instructions="""
You receive the body of an email to be sent.
First use subject_writer to write a subject, then html_converter to make the HTML body.
Finally use send_email_tool to send it with the subject, the plain text body and the HTML body.
""",
)


class SDRReply(BaseModel):
    text_body: str = Field(description="Plain-text reply to the prospect")
    html_body: str = Field(description="The same reply as simple HTML")
    end_conversation: bool = Field(description="True if the prospect clearly wants to stop, or a meeting is booked")


sdr_agent = Agent(
    name="SDR Agent",
    model=MODEL, model_settings=llm_settings(),
    output_type=SDRReply,
    instructions=intro + """
You are now an SDR (Sales Development Representative) in a live email conversation with a prospect.
Read the whole thread and reply to the prospect's latest message:
- Answer their questions honestly; don't invent specific prices or customer names.
- Handle objections with empathy and concrete value.
- Always move things forward with one clear next step (e.g. a 15-minute call - offer two time slots).
- Keep it short and human, under 120 words. Sign off as "Alex, ComplAI".
If the prospect asks to stop, reply politely and set end_conversation to true.
""",
)

prospect_simulator = Agent(
    name="Prospect Simulator",
    model=MODEL, model_settings=llm_settings(),
    instructions="""
You are Sam, CTO of a 40-person fintech startup that needs SOC2 for an enterprise deal in 6 months.
You are busy and a bit sceptical about AI tools. Reply to the latest sales email in the thread
in 1-4 sentences, like a real person: ask a pointed question, raise an objection, or
(if you're convinced) agree to a call. Reply with the email body only.
""",
)


# ---------------------------------------------------------------------------
# Part 1: Orchestration via CODE
# ---------------------------------------------------------------------------

async def code_orchestration() -> str:
    message = "Write a cold sales email"
    with trace("SDR - code orchestration"):
        print("✍️  Drafting 3 emails in parallel...")
        results = await asyncio.gather(
            Runner.run(sales_agent1, message),
            Runner.run(sales_agent2, message),
            Runner.run(sales_agent3, message),
        )
        drafts = "Cold sales emails:\n\n" + "\n\nEmail:\n\n".join(r.final_output for r in results)

        best = (await Runner.run(sales_picker, drafts)).final_output
        print(f"\n🏆 Picked:\n{best}")

        # Evaluator-optimizer loop: code decides when to stop
        for round_no in range(1, MAX_REFINE_ROUNDS + 1):
            review: EmailFeedback = (await Runner.run(email_critic, best)).final_output
            print(f"\n🔍 Round {round_no}: score {review.score}/10, approved={review.approved}\n   {review.feedback}")
            if review.approved:
                break
            best = (await Runner.run(email_refiner, f"Draft:\n{best}\n\nFeedback:\n{review.feedback}")).final_output
            print(f"\n✨ Refined email:\n{best}")
    return best


# ---------------------------------------------------------------------------
# Part 2: Orchestration via LLM (tools + handoff)
# ---------------------------------------------------------------------------

async def llm_orchestration():
    description = "Write a cold sales email. In the input, just instruct it to write a sales email."
    tools = [
        sales_agent1.as_tool(tool_name="sales_email_writer_1", tool_description=description),
        sales_agent2.as_tool(tool_name="sales_email_writer_2", tool_description=description),
        sales_agent3.as_tool(tool_name="sales_email_writer_3", tool_description=description),
        email_critic.as_tool(tool_name="email_critic", tool_description="Review an email; returns a score, approval and feedback"),
        email_refiner.as_tool(tool_name="email_refiner", tool_description="Rewrite an email given the draft and the feedback"),
    ]

    sales_manager = Agent(
        name="Sales Manager",
        model=MODEL,
        tools=tools,
        handoffs=[email_manager],
        model_settings=llm_settings(tool_choice="required"),
        instructions=f"""
You are a Sales Manager at ComplAI. Your goal is to send out the single best cold sales email.

1. Generate drafts: use all three sales_email_writer tools. Do not proceed until all three drafts are ready.
2. Select: choose the single most effective draft with your own judgment. Never write the email yourself.
3. Refine: send the chosen email to email_critic. If it is not approved, pass the draft and the
   feedback to email_refiner, then review again. Do at most {MAX_REFINE_ROUNDS} refine rounds.
4. Handoff: hand off ONLY the final email body to the Email Manager, who will format and send it.
""",
    )

    with trace("SDR - LLM orchestration"):
        result = await Runner.run(sales_manager, "Send out a cold sales email addressed to 'Dear CEO'", max_turns=30)
    print(f"\n✅ Done. Last agent: {result.last_agent.name}")


# ---------------------------------------------------------------------------
# Part 3: Live conversation - send via Resend, handle replies, SDR keeps it going
# ---------------------------------------------------------------------------

async def conversation(simulate: bool):
    best = await code_orchestration()

    # Let the Email Manager format and send the refined email
    with trace("SDR - send first email"):
        await Runner.run(email_manager, best)

    for turn in range(1, MAX_CONVERSATION_TURNS + 1):
        history = "\n\n---\n\n".join(thread["history"])

        if simulate:
            reply = (await Runner.run(prospect_simulator, f"Email thread so far:\n\n{history}")).final_output
            print(f"\n📨 (simulated) Prospect reply:\n{reply}")
        else:
            reply = await asyncio.to_thread(wait_for_reply)
            if not reply:
                break
        thread["history"].append(f"Prospect:\n{reply}")

        with trace(f"SDR - SDR turn {turn}"):
            history = "\n\n---\n\n".join(thread["history"])
            answer: SDRReply = (await Runner.run(sdr_agent, f"Email thread so far:\n\n{history}")).final_output

        print(f"\n🤖 SDR reply:\n{answer.text_body}")
        if simulate:
            thread["history"].append(f"SDR (us):\n{answer.text_body}")
        else:
            send_email(f"Re: {thread['subject']}", answer.text_body, answer.html_body)

        if answer.end_conversation:
            print("\n🏁 Conversation ended by SDR.")
            break


# ---------------------------------------------------------------------------
# Part 4: Gradio chatbot - pick an objective + orchestration style, get the best email
# ---------------------------------------------------------------------------

PRESET_OBJECTIVES = [
    "Write a cold sales email",
    "Cold email to the CTO of a 40-person fintech startup that needs SOC2 in 6 months for an enterprise deal",
    "Re-engage a lead who tried our free trial last month but never upgraded",
    "Invite a VP of Engineering to a 20-minute live demo of ComplAI next week",
    "Follow up with a prospect who said they are already using spreadsheets for SOC2",
    "Email a startup founder who just failed their first SOC2 audit",
]

ORCHESTRATION_MODES = {
    "Via Code": "Python runs 3 writers in parallel → picker → critic/refiner loop → sends",
    "Via LLM (agents as tools)": "Sales Manager LLM calls writers, critic, refiner and the send tool itself",
    "Via Handoff": "Sales Manager LLM drafts with writer tools, then hands off to the Email Manager to format & send",
}

WRITER_TOOL_DESCRIPTION = "Write a cold sales email. Pass the sales objective as the input."


def _writer_tools():
    return [
        sales_agent1.as_tool(tool_name="sales_email_writer_1", tool_description=WRITER_TOOL_DESCRIPTION),
        sales_agent2.as_tool(tool_name="sales_email_writer_2", tool_description=WRITER_TOOL_DESCRIPTION),
        sales_agent3.as_tool(tool_name="sales_email_writer_3", tool_description=WRITER_TOOL_DESCRIPTION),
    ]


def _review_tools():
    return [
        email_critic.as_tool(tool_name="email_critic", tool_description="Review an email; returns a score, approval and feedback"),
        email_refiner.as_tool(tool_name="email_refiner", tool_description="Rewrite an email given the draft and the feedback"),
    ]


def _step(title: str, content: str = "") -> dict:
    """A collapsible step bubble in the chatbot."""
    return {"role": "assistant", "content": content or "…", "metadata": {"title": title}}


def _short(text, limit: int = 600) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:limit] + " …"


async def ui_via_code(objective: str):
    """Code decides every step. Yields chat bubbles as it goes."""
    message = f"Write a cold sales email. Sales objective: {objective}"
    yield _step("✍️ Drafting 3 emails in parallel (Professional, Humorous, Executive)")
    results = await asyncio.gather(
        Runner.run(sales_agent1, message),
        Runner.run(sales_agent2, message),
        Runner.run(sales_agent3, message),
    )
    for agent, result in zip([sales_agent1, sales_agent2, sales_agent3], results):
        yield _step(f"📝 Draft - {agent.name}", result.final_output)

    drafts = "Cold sales emails:\n\n" + "\n\nEmail:\n\n".join(r.final_output for r in results)
    best = (await Runner.run(sales_picker, drafts)).final_output
    yield _step("🏆 Sales Picker chose", best)

    for round_no in range(1, MAX_REFINE_ROUNDS + 1):
        review: EmailFeedback = (await Runner.run(email_critic, best)).final_output
        yield _step(f"🔍 Critic round {round_no}: {review.score}/10 {'✅ approved' if review.approved else '❌ needs work'}",
                    review.feedback)
        if review.approved:
            break
        best = (await Runner.run(email_refiner, f"Draft:\n{best}\n\nFeedback:\n{review.feedback}")).final_output
        yield _step(f"✨ Refined email (round {round_no})", best)

    yield _step("📮 Writing subject + HTML and sending")
    subject, html = await asyncio.gather(Runner.run(subject_writer, best), Runner.run(html_converter, best))
    send_email(subject.final_output.strip(), best, html.final_output)


async def _stream_agent(agent: Agent, task: str):
    """Run an agent streamed and turn tool calls / handoffs into chat bubbles."""
    result = Runner.run_streamed(agent, task, max_turns=30)
    async for event in result.stream_events():
        if event.type == "agent_updated_stream_event":
            yield _step(f"🤝 Now running: {event.new_agent.name}")
        elif event.type == "run_item_stream_event":
            item = event.item
            if event.name == "tool_called":
                yield _step(f"🛠️ {agent_name(item)} calls tool `{getattr(item.raw_item, 'name', 'tool')}`")
            elif event.name == "tool_output":
                yield _step("📤 Tool result", _short(item.output))
            elif event.name == "handoff_requested":
                yield _step(f"➡️ {agent_name(item)} requests a handoff")


def agent_name(item) -> str:
    return getattr(item.agent, "name", "Agent")


async def ui_via_llm(objective: str):
    """One manager LLM plans and calls everything as tools - no handoff."""
    manager = Agent(
        name="Sales Manager",
        model=MODEL,
        tools=_writer_tools() + _review_tools() + list(email_manager.tools),
        model_settings=llm_settings(tool_choice="required"),
        instructions=f"""
You are a Sales Manager at ComplAI. Your goal is to send out the single best cold sales email for the objective.

1. Use all three sales_email_writer tools, passing them the objective. Wait for all three drafts.
2. Choose the single most effective draft. Never write the email yourself.
3. Send it to email_critic. If not approved, pass the draft and feedback to email_refiner and review again
   (at most {MAX_REFINE_ROUNDS} rounds).
4. Use subject_writer for a subject and html_converter for the HTML body.
5. Use send_email_tool exactly once with the subject, plain text body and HTML body. Then reply "Done".
""",
    )
    async for bubble in _stream_agent(manager, f"Sales objective: {objective}"):
        yield bubble


async def ui_via_handoff(objective: str):
    """Manager LLM drafts with tools, then hands control to the Email Manager."""
    manager = Agent(
        name="Sales Manager",
        model=MODEL, model_settings=llm_settings(),
        tools=_writer_tools() + _review_tools(),
        handoffs=[email_manager],
        instructions=f"""
You are a Sales Manager at ComplAI. You get your team to draft emails, then hand off the best one.

1. Use all three sales_email_writer tools, passing them the objective. Wait for all three drafts.
2. Choose the single most effective draft. Never write the email yourself.
3. Send it to email_critic. If not approved, pass the draft and feedback to email_refiner and review again
   (at most {MAX_REFINE_ROUNDS} rounds).
4. Hand off ONLY the final email body to the Email Manager, who will format and send it.
""",
    )
    async for bubble in _stream_agent(manager, f"Sales objective: {objective}"):
        yield bubble


UI_RUNNERS = {
    "Via Code": ui_via_code,
    "Via LLM (agents as tools)": ui_via_llm,
    "Via Handoff": ui_via_handoff,
}


REPLY_SOURCES = ["Check my inbox (real reply)", "Simulate the prospect (AI)", "Type the prospect's reply"]


def launch_ui():
    import gradio as gr

    async def sdr_turn(source, manual_reply, wait_minutes, send_for_real, history):
        history = history or []
        if not thread["subject"]:
            history.append({"role": "assistant", "content": "⚠️ Send an email first with 🚀 Generate best email."})
            yield history, gr.update()
            return
        settings["send_enabled"] = send_for_real

        # 1. Get the prospect's reply
        if source == REPLY_SOURCES[0]:
            if not send_for_real:
                history.append({"role": "assistant", "content": "⚠️ Dry run is on - no real email was sent, so no real reply can arrive."})
                yield history, gr.update()
                return
            history.append(_step(f"⏳ Waiting up to {int(wait_minutes)} min for a reply in {EMAIL_ADDRESS}",
                                 "Reply to the email in your inbox (use Reply, keep the subject)."))
            yield history, gr.update()
            reply = await asyncio.to_thread(wait_for_reply, int(wait_minutes * 60))
            if not reply:
                history.append({"role": "assistant", "content": "⌛ No reply yet. Click the button again to keep waiting."})
                yield history, gr.update()
                return
        elif source == REPLY_SOURCES[1]:
            history.append(_step("🎭 Prospect Simulator is writing a reply"))
            yield history, gr.update()
            reply = (await Runner.run(prospect_simulator, "Email thread so far:\n\n" + "\n\n---\n\n".join(thread["history"]))).final_output
        else:
            reply = (manual_reply or "").strip()
            if not reply:
                history.append({"role": "assistant", "content": "⚠️ Type the prospect's reply first."})
                yield history, gr.update()
                return

        thread["history"].append(f"Prospect:\n{reply}")
        history.append({"role": "user", "content": f"📨 **Prospect:**\n\n{reply}"})
        yield history, ""

        # 2. SDR answers in the same thread
        history.append(_step("🤖 SDR Agent is writing a response"))
        yield history, ""
        with trace("SDR UI - SDR turn"):
            answer: SDRReply = (await Runner.run(sdr_agent, "Email thread so far:\n\n" + "\n\n---\n\n".join(thread["history"]))).final_output
        send_email(f"Re: {thread['subject']}", answer.text_body, answer.html_body)

        content = f"🤖 **SDR (Alex):**\n\n{answer.text_body}\n\n---\n📧 {last_email['provider']} → `{last_email['to']}`"
        if answer.end_conversation:
            content += "\n\n🏁 The SDR thinks this conversation is done (meeting booked or prospect opted out)."
        history.append({"role": "assistant", "content": content})
        yield history, ""

    async def run(preset, custom, mode, send_for_real, history):
        objective = (custom or "").strip() or preset
        history = (history or []) + [{"role": "user", "content": f"**{mode}**\n\n{objective}"}]
        yield history, ""

        # Fresh thread for every run
        thread.update(subject=None, message_ids=[], history=[], processed=set(), last_sent_at=0.0)
        last_email.clear()
        settings["send_enabled"] = send_for_real

        try:
            with trace(f"SDR UI - {mode}"):
                async for bubble in UI_RUNNERS[mode](objective):
                    history.append(bubble)
                    yield history, ""
        except Exception as e:
            history.append({"role": "assistant", "content": f"⚠️ Error: `{e}`"})
            yield history, ""
            return

        if last_email:
            history.append({
                "role": "assistant",
                "content": f"### ✅ Best email\n**Subject:** {last_email['subject']}\n\n{last_email['text_body']}\n\n"
                           f"---\n📧 {last_email['provider']} → `{last_email['to']}`",
            })
            yield history, last_email["html_body"]
        else:
            history.append({"role": "assistant", "content": "⚠️ The agents finished without sending an email."})
            yield history, ""

    with gr.Blocks(title="ComplAI SDR") as demo:
        gr.Markdown("# ComplAI Sales Email Agent\nPick or type a sales objective, choose how the agents are orchestrated, and get the best email.")
        with gr.Row():
            with gr.Column(scale=1):
                preset = gr.Dropdown(PRESET_OBJECTIVES, value=PRESET_OBJECTIVES[0], label="Ready-made sales objective")
                custom = gr.Textbox(label="…or write your own objective", lines=3,
                                    placeholder="e.g. Cold email to a healthcare SaaS CISO about HIPAA + SOC2")
                mode = gr.Radio(list(ORCHESTRATION_MODES), value="Via Code", label="Orchestration")
                mode_info = gr.Markdown(ORCHESTRATION_MODES["Via Code"])
                send_for_real = gr.Checkbox(value=True, label=f"Send for real via {'Resend' if RESEND_API_KEY else 'SMTP'} to {PROSPECT_EMAIL}")
                go = gr.Button("🚀 Generate best email", variant="primary")
                clear = gr.Button("Clear chat")
            with gr.Column(scale=2):
                chat = gr.Chatbot(label="Agent activity", height=600)
                with gr.Accordion("💬 Continue the conversation (SDR)", open=True):
                    reply_source = gr.Radio(REPLY_SOURCES, value=REPLY_SOURCES[1], label="Where does the prospect's reply come from?")
                    manual_reply = gr.Textbox(label="Prospect's reply", lines=3, visible=False,
                                              placeholder="e.g. We already use spreadsheets. How is this different?")
                    wait_minutes = gr.Slider(1, 15, value=5, step=1, label="Max minutes to wait for the inbox reply", visible=False)
                    sdr_go = gr.Button("🤖 Get reply → SDR responds", variant="secondary")
                preview = gr.HTML(label="HTML preview")

        mode.change(lambda m: ORCHESTRATION_MODES[m], mode, mode_info)
        go.click(run, [preset, custom, mode, send_for_real, chat], [chat, preview])
        clear.click(lambda: ([], ""), None, [chat, preview])
        reply_source.change(
            lambda s: (gr.update(visible=s == REPLY_SOURCES[2]), gr.update(visible=s == REPLY_SOURCES[0])),
            reply_source, [manual_reply, wait_minutes],
        )
        sdr_go.click(sdr_turn, [reply_source, manual_reply, wait_minutes, send_for_real, chat], [chat, manual_reply])

    demo.launch(inbrowser=True)


def main():
    parser = argparse.ArgumentParser(description="AI Sales SDR: multi-agent outreach + Resend + SDR conversation")
    parser.add_argument("--mode", choices=["ui", "code", "llm", "full"], default="ui")
    parser.add_argument("--simulate", action="store_true", help="An AI plays the prospect instead of waiting for real replies")
    args = parser.parse_args()

    if not RESEND_API_KEY:
        print("⚠️  RESEND_API_KEY not set - falling back to SMTP")

    if args.mode == "ui":
        launch_ui()
    elif args.mode == "code":
        asyncio.run(code_orchestration())
    elif args.mode == "llm":
        asyncio.run(llm_orchestration())
    else:
        asyncio.run(conversation(args.simulate))


if __name__ == "__main__":
    main()
