# AI Sales SDR

**A multi-agent sales development representative that writes, critiques, refines and sends cold emails, then holds a real conversation with prospects who reply.**

AI Sales SDR is built on the [OpenAI Agents SDK](https://github.com/openai/openai-agents-python). Several AI writers with different styles draft competing emails. The best draft is chosen and improved through a critic–refiner loop, then sent as a polished HTML email through Resend. When the prospect replies, an SDR agent reads the reply from your inbox and answers it in the same email thread.

The demo company is **ComplAI**, a fictional SaaS product for SOC 2 compliance. You can rebrand it by editing the agent instructions.

![Python](https://img.shields.io/badge/python-3.12-blue)
![OpenAI Agents SDK](https://img.shields.io/badge/OpenAI-Agents%20SDK-412991)
![LLM](https://img.shields.io/badge/LLM-OpenAI%20%7C%20Claude%20%7C%20OpenRouter%20%7C%20Grok%20%7C%20Gemini%20%7C%20Ollama-6b4fbb)
![Gradio](https://img.shields.io/badge/Gradio-6-ff7c00)
![Resend](https://img.shields.io/badge/email-Resend-black)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Screenshots

**Start screen.** Pick an objective, choose how the agents are orchestrated, and decide whether to really send.

![AI Sales SDR start screen](docs/screenshots/home.png)

**After a run.** The agent activity panel streams every step and ends with the best email. Below it are the SDR conversation controls and a live HTML preview of the email exactly as the prospect will see it. This run was a dry run, so nothing was sent.

![AI Sales SDR after generating an email](docs/screenshots/run.png)

---

## Features

- **Bring your own LLM.** Run every agent on OpenAI, Claude, Grok, Gemini, Llama and more through OpenRouter, xAI, Gemini or a local Ollama model.
- **Competing writers.** Professional, humorous and executive-style sales agents draft emails in parallel.
- **Best-of-N selection.** A picker agent chooses the strongest draft.
- **Critic–refiner loop.** A critic scores the email with structured feedback, and a refiner rewrites it until it is approved or the round limit is reached.
- **Three orchestration strategies,** selectable at runtime:
  - **Via Code:** Python controls the workflow deterministically.
  - **Via LLM:** a Sales Manager agent uses the writers, critic and refiner as tools and decides the flow itself.
  - **Via Handoff:** the Sales Manager drafts, then hands off to an Email Manager that writes the subject, converts the body to HTML and sends it.
- **Real email delivery** through Resend, with automatic SMTP fallback and correct threading headers (`Message-ID`, `In-Reply-To`, `References`).
- **Autonomous reply handling.** The inbox is polled over IMAP, and quoted text and the app's own messages are ignored. The SDR agent replies in the same thread for several turns.
- **Flexible conversation testing.** The prospect's reply can come from your real inbox, from an AI-simulated prospect, or from text you type.
- **Live agent visibility.** The web UI streams every agent step and shows an HTML preview of the email.
- **Dry-run mode.** Run the whole pipeline without sending anything.
- **Tracing.** Every run is traced in the OpenAI platform dashboard.

---

## How it works

```mermaid
flowchart LR
    O[Sales objective] --> W1[Professional writer]
    O --> W2[Humorous writer]
    O --> W3[Executive writer]
    W1 & W2 & W3 --> P[Picker]
    P --> C{Critic}
    C -->|needs work| R[Refiner] --> C
    C -->|approved| M[Email Manager<br/>subject · HTML]
    M --> S[Resend / SMTP]
    S --> X[Prospect inbox]
    X -->|reply| I[IMAP reader]
    I --> SDR[SDR agent] --> S
```

### Agents

| Agent | Responsibility |
|---|---|
| Professional / Humorous / Executive Sales Agent | Draft cold emails in distinct voices |
| Sales Picker | Selects the most effective draft |
| Email Critic | Scores the draft and returns structured feedback |
| Email Refiner | Rewrites the draft based on that feedback |
| Subject Writer | Writes a compelling subject line |
| HTML Converter | Turns plain text into a clean HTML email |
| Email Manager | Formats and sends the final email |
| Sales Manager | Orchestrates the writers and reviewers (LLM and handoff modes) |
| SDR Agent | Answers prospect replies within the thread |
| Prospect Simulator | Plays a realistic prospect for testing |

---

## Getting started

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- An API key for any supported LLM provider (or a local Ollama install)
- A [Resend](https://resend.com) API key and/or an email account with an app password (for SMTP/IMAP)

### Installation

```bash
git clone https://github.com/<your-username>/ai-sales-sdr.git
cd ai-sales-sdr
cp .env.example .env    # add your keys
uv sync
```

### Run

```bash
uv run app.py                          # web app (default)
uv run app.py --mode code              # code orchestration in the terminal
uv run app.py --mode llm               # LLM orchestration in the terminal
uv run app.py --mode full              # send, then wait for real replies
uv run app.py --mode full --simulate   # an AI plays the prospect
```

In the web app:

1. Pick a ready-made objective or write your own.
2. Choose an orchestration mode. Untick **Send for real** for a dry run.
3. Watch the agents work and review the HTML preview.
4. Continue the conversation with a real inbox reply, a simulated prospect, or your own text.

---

## Choose your LLM provider

Set `LLM_PROVIDER` in `.env` and add that provider's key. All agents switch together.

| Provider | `LLM_PROVIDER` | Key | Default model |
|---|---|---|---|
| OpenAI | `openai` | `OPENAI_API_KEY` | `gpt-5.4-mini` |
| OpenRouter (Claude, GPT, Grok, Llama, Mistral…) | `openrouter` | `OPENROUTER_API_KEY` | `openai/gpt-5.4-mini` |
| xAI Grok | `grok` | `XAI_API_KEY` | `grok-4.7` |
| Google Gemini | `gemini` | `GEMINI_API_KEY` | `gemini-2.5-flash` |
| Ollama (local) | `ollama` | none | `llama3.2` |

**Using Claude.** The OpenAI Agents SDK talks to OpenAI-style APIs, so Claude runs through OpenRouter:

```bash
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-...
LLM_MODEL=anthropic/claude-haiku-5.5     # or anthropic/claude-sonnet-5.5, anthropic/claude-opus-5.5
```

**Using Grok.**

```bash
LLM_PROVIDER=grok
XAI_API_KEY=xai-...
```

For providers other than OpenAI, the app switches the Agents SDK to the Chat Completions API and caps output tokens (`LLM_MAX_TOKENS`, default 4096). It also turns off tracing when there is no OpenAI key, because traces are uploaded to the OpenAI dashboard. Models that don't support forced tool calls (Claude Opus 5.5, Sonnet 5.5) are handled automatically.

---

## Configuration

| Variable | Required | Description |
|---|---|---|
| `LLM_PROVIDER` | | `openai` (default), `openrouter`, `grok`, `gemini`, `ollama` |
| `LLM_MODEL` | | Override the provider's default model |
| Provider key | ✅ | `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `XAI_API_KEY` or `GEMINI_API_KEY` |
| `RESEND_API_KEY` | | Resend API key; if empty, SMTP is used |
| `RESEND_FROM` | | Sender, e.g. `ComplAI <onboarding@resend.dev>` |
| `PROSPECT_EMAIL` | | Recipient of the cold email |
| `EMAIL_ADDRESS` | | Inbox that receives replies (also used as `Reply-To`) |
| `EMAIL_APP_PASSWORD` | | App password for IMAP/SMTP |
| `EMAIL_SMTP_SERVER` | | e.g. `smtp.gmail.com` |
| `IMAP_SERVER` | | Optional; derived from the SMTP server when empty |

When the sender is `onboarding@resend.dev`, Resend only delivers to your own Resend account address. Verify a domain to email anyone else.

For Gmail, enable 2-step verification and create an [app password](https://myaccount.google.com/apppasswords) for IMAP/SMTP access.

---

## Tech stack

**OpenAI Agents SDK** (agents, tools, handoffs, structured outputs, tracing) · **OpenRouter / xAI / Gemini / Ollama** · **Gradio 6** · **Resend** · **IMAP/SMTP** · **Pydantic** · **asyncio**

---

## Responsible use

This project is meant for demos and for prospects who expect to hear from you. Follow anti-spam laws (CAN-SPAM, GDPR, PECR) and get consent before emailing real prospects at scale.

---

## License

Released under the MIT License.
