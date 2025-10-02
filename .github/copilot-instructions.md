# AI Assistant Instructions for This Repo

Purpose: Enable fast, correct edits to this Discord/Web LLM bot. Favor small, targeted changes that preserve config‑driven behavior and existing patterns.

## Architecture at a glance
- Entrypoint: `src/bot_app.py` loads `config.yaml`, configures logging, builds a `MessageRouter`, and runs:
  - Discord client (if `bot_type.method` is `DISCORD`/`BOTH`).
  - FastAPI web server (if `bot_type.method` is `WEB`/`BOTH`) at `http.html_host:html_port`.
- Core flow (both Discord and Web): message → `MessageRouter.handle_message` or `build_batch_reply` → `ParticipationPolicy` → prompt assembly (`PromptTemplateEngine` + `PersonaService` + optional `LoreService`) → `LLM` call → post reply → update `ConversationMemory` and conversation-mode state.
- Router build is centralized in `http_app.build_router_from_config(cfg)` and mirrored in `bot_app.py`.

## Web mode specifics
- App: `src/http_app.py` mounts static UI (`/static` → `src/web/static`) and serves `/`.
- Endpoints: `/chat` (POST), `/reset` (POST), `/web-config` (GET), `/health` (GET).
  - `/chat` constructs a web event and calls `router.build_batch_reply`; single room id is `"web-room"` by default.
  - `/web-config` returns `{ bot_name, default_user_name, token_required }`.
  - `/reset` clears `ConversationMemory.clear("web-room")` and `ConversationBatcher.clear("web-room")`.
- UI: `src/web/static/{index.html,app.js}` renders Markdown (safe subset), images/links, status line, and a Reset button. If `http.bearer_token` is set, a token field appears and headers include `Authorization: Bearer <token>`; token persists in `localStorage`.

## Prompt composition (what to maintain)
- Use paths from config: `context.system_prompt_path`, optional `context.system_prompt_path_nsfw`, `context.persona_path`, and `context.context_template_path`.
- If lore is enabled (`context.lore.enabled`): insert one system block after the system prompt: “You may use the following background context…” with `[Lore]`.
- If `context.use_template`: render the context template as a system message and keep a small raw history tail (`context.keep_history_tail`).
- Budget: `model.context_window` minus `model.max_tokens`; evict oldest history first; truncate current user content last.

## Participation & conversation mode
- General chat only in `participation.general_chat.allowed_channels`; `response_chance_override` forces 100% chance (cooldowns still apply).
- Mentions or name aliases (from `participation.name_aliases`) always allowed.
- After the bot replies, conversation mode uses `window_seconds` and `max_messages`; `ConversationBatcher` drains buffers and posts one summary reply per interval.

## Config keys agents should know
- Run mode: `bot_type.method` = `DISCORD|WEB|BOTH`.
- Web server: `http.html_host` (default `127.0.0.1`), `http.html_port` (default `8005`).
- Web auth (optional): `http.bearer_token` → required on `/chat` (and used by UI when present).
- Models: `model.*` (temperature, top_p, max_tokens, context_window, models[], retries, concurrency, http_referer, x_title).
- Logging: `LOG_LEVEL`, `LIB_LOG_LEVEL`, `LOG_PROMPTS`, `LOG_TO_OUTPUT`.

## Logging & correlation
- Common lines: `decision …`, `llm-start/llm-finish …`, `tokenizer-summary …`, `lore-include` | `lore-limit-reached`.
- Prompt dumps when `LOG_PROMPTS=true` → `logs/prompts-YYYYMMDD-HHMMSS/`.
- Correlation: `utils/correlation.make_correlation_id(channel_id, message_id)`; the web path attaches this per request.

## Dev workflow (Windows/PowerShell)
```powershell
$env:PYTHONPATH = "src"
python -m src.bot_app
```
Secrets in `.env`: `DISCORD_TOKEN`, `OPENROUTER_API_KEY`.

## Patterns & conventions for edits
- Add config → update `config.yaml` and provide a minimal getter in `ConfigService` (keep safe defaults; retain backward compatibility when keys move).
- Prefer config flags for behavior changes; keep decision logs via `ParticipationPolicy`.
- Providers: implement `LLMClient`-compatible classes under `src/llm/`, wire in `bot_app.py`, keep OpenRouter as default.
- Web UI: serve assets from `src/web/static` (do not inline large JS/HTML in Python); keep `/web-config` small.
- Never log secrets; keep external URLs in UI to `http(s)` only.
