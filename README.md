# Discord LLM Bot

A configurable Discord chatbot using OpenRouter models. Includes conversation mode, prompt templating, optional lore (JSON + Markdown), and structured logging. Also ships with a lightweight Web Chat UI. Made with Copilot/GPT-5.

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

1. On first run, `.env` is auto-created from `.env.example` if missing, `config.yaml` will also be created from `config.example.yaml`. Fill in `.env` values and adjust `config.yaml`:

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

### Web UI (optional)

- Enable in `config.yaml`:

```yaml
bot_type:
  method: BOTH   # DISCORD | WEB | BOTH

http:
  html_port: 8005
  html_host: 127.0.0.1  # default; set 0.0.0.0 to expose on your LAN
  bearer_token: ""     # optional; when set, required on /chat and /reset
```

- Launch as usual; then open the Web UI at:
  - <http://localhost:8005/>

Routes

- `/`, `/index`, `/index.html`: serve the Web Chat page (`src/web/static/index.html`).
- `/static/*`: static assets (JS, CSS, images) under `src/web/static/`.
- `/web-config`: small JSON for UI labels (e.g., `bot_name` from `participation.name_aliases`).
- `/chat`: POST endpoint the UI calls with `{ content, user_name, user_id }`.
  - Optional: `channel_id` (defaults to "web-room").
  - If `http.auth.bearer_token` is set, include header: `Authorization: Bearer <token>`.
- `/health`: returns `{ ok: true }`.
- `/favicon.ico`: returns 204 by default (add your own icon to `src/web/static/` if desired).

Behavior

- Status line shows "Ready for new Message." when idle and "Message sent, awaiting response." after sending.
- Auto-scrolls the log panel on updates.
- Ensures one blank line between user and bot turns.
- Respects `LOG_PROMPTS`: prompts/responses for web chat are logged under `logs/prompts-YYYYMMDD-HHMMSS/`.

## Features

- Models: ordered list via `model.models` with sequential fallback; optional `allow_auto_fallback` using `openrouter/auto`.
- Conversation mode: windowed follow-ups with batching; recent-context clustering + thread affinity.
- Prompt templating: system + persona + optional lore; context template renders a compact history block; token budgeting respects `model.context_window` and `model.max_tokens`.
- Lore: load SillyTavern-style JSON and Markdown; capped by `context.lore.max_fraction` of prompt budget.
- Vision (image input): attach up to `vision.max_images` when enabled; applies by scope (`mentions`, `replies`, `general_chat`, `batch`). Falls back to text if gated by scope or model.
- Logging: structured INFO/DEBUG lines; optional prompt dumps with `LOG_PROMPTS=true` (may contain sensitive info).
  - Applies to Discord and Web UI paths.

## Troubleshooting

- 429 rate limits: the client retries; consider adding more models to `model.models`.
- No responses: check `logs/log.log` and verify `.env` secrets.

## Notes

- System/persona/lore files are hot-reloaded from paths in `config.yaml`.
- NSFW channels can switch to an alternate system prompt when configured.
- Keep `LOG_PROMPTS=false` by default to avoid writing sensitive content to disk.
