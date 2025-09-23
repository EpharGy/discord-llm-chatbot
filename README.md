# Discord LLM Bot

A configurable Discord chatbot using OpenRouter models. Includes conversation mode, prompt templating, optional lore (JSON + Markdown), and structured logging. Made with Copilot/GPT-5.

## Prerequisites

- Python 3.10+
- A Discord Bot Token with the required intents (Message Content if analyzing text)

## Setup

1. Create a virtual environment and install deps:

```powershell
python -m venv .venv
. .venv\Scripts\Activate.ps1
pip install -e .
```

1. On first run, `.env` is auto-created from `.env.example` if missing, `config.yaml` will also be created from `config.example.yaml`. Fill in `.env` values and adjust `config.yaml`.:

```dotenv
DISCORD_TOKEN=your-token
OPENROUTER_API_KEY=your-openrouter-key
# Optional
DISCORD_CLIENT_ID=your-app-id
```

1. Review `config.yaml` and ensure `discord.intents.message_content` matches your needs and is enabled in the Developer Portal.

## Run (Windows PowerShell)

```powershell
$env:PYTHONPATH = "src"
python -m src.bot_app
```

If the bot logs in successfully, invite it to your server and mention it by one of the name aliases in `config.yaml` (e.g., "assistant").

## Features

- Model list via `model.models` with sequential fallback; optional `openrouter/auto` fallback.
- Conversation mode with batching and recent-context clustering + thread affinity.
- Prompt templating and lore insertion (SillyTavern-style JSON and Markdown) with budgeting.
- Logs: `LOG_LEVEL=INFO|DEBUG|FULL`; `LOG_ERRORS=true` writes `logs/errors.log`; optional prompt dumps.

## Troubleshooting

- 429 rate limits: the client retries; consider adding more models to `model.models`.
- No responses: check `logs/errors.log` and verify `.env` secrets.
