# Discord LLM Bot (Discord + Web UI)

A configurable Discord/Web chatbot. Uses OpenRouter by default and can target a local OpenAI‑compatible backend (LM Studio, llama.cpp, etc.). Features conversation mode, prompt templating, persona packs, optional lore (JSON + Markdown), and structured logging. Ships with a lightweight Web Chat UI.

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

If the bot logs in successfully, invite it to your server and mention it by one of the name aliases from your selected persona (see Persona system).

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
- `/web-config`: small JSON for UI labels and capabilities (`bot_name`, `providers`, `default_provider`, whether a token is required).
- `/chat`: POST endpoint the UI calls with `{ content, user_name, user_id, provider? }`.
  - Optional: `channel_id` (defaults to "web-room").
  - If `http.auth.bearer_token` is set, include header: `Authorization: Bearer <token>`.
- `/reset`: POST → clears conversation state for the web room.
- `/health`: returns `{ ok: true }`.
- `/favicon.ico`: returns 204 by default (add your own icon to `src/web/static/` if desired).

Behavior

- Full-height layout: conversation fills the middle; controls on top (Theme → Name → Provider → API token), input above the status line at the bottom.
- Status line: green "Ready for new Message." when idle; red "Message sent, awaiting response." during requests.
- Provider select: populated from `/web-config`, persists in localStorage, and is sent with the chat request.
- API token: placeholder hint (italic/grey). If non-empty, the UI always sends `Authorization: Bearer <token>`.
- Auto-scrolls the log panel on updates. Respects `LOG_PROMPTS`: prompts/responses for web chat are logged under `logs/prompts-YYYYMMDD-HHMMSS/`.

Auth notes

- When `http.bearer_token` is set, the server requires it. The UI sends the header whenever the token field is non-empty (even if `/web-config` hasn’t been fetched yet), reducing 401s.

## Features

- Models: ordered list via `model.models` with sequential fallback; optional `allow_auto_fallback` using `openrouter/auto`.
- Conversation mode: windowed follow-ups with batching; recent-context clustering + thread affinity.
- Prompt templating: system + persona + optional lore; context template renders a compact history block; token budgeting respects `model.context_window` and `model.max_tokens`.
- Lore: global `context.lore.paths` merged with persona-provided lore; capped by `context.lore.max_fraction` of prompt budget.
- Vision (image input): attach up to `vision.max_images` when enabled; applies by scope (`mentions`, `replies`, `general_chat`, `batch`). Falls back to text if gated by scope or model.
- Logging: structured INFO/DEBUG lines; optional prompt dumps with `LOG_PROMPTS=true` (may contain sensitive info).
  - Applies to Discord and Web UI paths.
  - Provider logs are unified: only one `[llm-finish]` at INFO from the router. For OpenAI‑compatible backends, the model field is hidden to avoid confusion.

## Persona system

- Select persona with a single key in `config.yaml`:

```yaml
persona: default  # uses personas/default/
```

- Each persona is a folder under `personas/<name>/` with a YAML file (prefer `default.yaml`, fallback `<name>.yaml`). Example:

```yaml
name_aliases:
  - default
  - '@default'
persona: default.md
lore:
  - default.json
  - default.md
system_prompt: System.txt
system_prompt_nsfw: System_nsfw.txt
```

- Resolution & fallbacks:
  - system_prompt/context_template/persona file are loaded from the selected persona; if missing, fall back to the default persona; finally, hard defaults under `personas/default/`.
  - name_aliases come from the persona; if missing, fall back to the default persona.
  - Lore paths from persona YAML are merged with global `context.lore.paths`.
  - Startup logs warn if referenced persona files are missing and which fallbacks are used.

- Distribution: `.gitignore` ships only `personas/default/**` by default (`personas/*` ignored, `!personas/default/*` allowed).

## NSFW behavior

- Toggle NSFW prompt usage via:

```yaml
participation:
  allow_nsfw: true  # false → always use the normal system prompt
```

## Troubleshooting

- 401 on Web UI: ensure `http.bearer_token` matches what you enter; the UI now always sends the header when a token is present.
- 429 rate limits: the client retries; consider adding more models to `model.models`.
- No responses: check `logs/log.log` and verify `.env` secrets.

## Notes

- Personas drive system/prompt paths; the app falls back to the default persona when needed. Lore is merged (global + persona).
- NSFW channels can switch to an alternate system prompt when `participation.allow_nsfw=true` and an NSFW system prompt is provided.
- Keep `LOG_PROMPTS=false` by default to avoid writing sensitive content to disk.
