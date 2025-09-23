# AI Assistant Instructions for This Repo

Purpose: Enable fast, correct edits to this Discord LLM bot. Favor small, targeted changes that preserve existing patterns and config-driven behavior.

## Project map and flow
- Entrypoint: `src/bot_app.py` wires all services, loads `config.yaml`, sets logging, starts Discord client, and runs two background loops (mentions queue and conversation batching).
- Event flow: Discord message → `MessageRouter.handle_message` → `ParticipationPolicy.should_reply` → prompt assembly (`PromptTemplateEngine`, persona, optional lore, history) → `LLMClient` call → post reply → memory/conv-mode updates.
- Key components:
  - `ConfigService` (load/reload config + getters)
  - `LoggerFactory` (structured logs); use `fmt()` from `utils/logfmt.py`
  - `ParticipationPolicy` (rate limits, triggers, channel allowlist, `response_chance_override` for 100% chance)
  - `ConversationMemory` (recent msgs, cooldown data, conv-mode window; tracks responded message IDs)
  - `ConversationBatcher` (batch non-mentions during conv-mode)
  - `PromptTemplateEngine` + `PersonaService` (system + persona + context template)
  - `LoreService` (optional SillyTavern JSON; inserted as a system block after system prompt)
  - `TokenizerService` (heuristic counts + truncation)
  - `LLM` (`src/llm/openrouter_client.py`) with retries and `X-Title: Discord LLM Bot` header

## Prompt composition (what to maintain)
- System prompt from `prompts/system.txt`.
- If lore is enabled: insert a single system message right after system prompt: "You may use the following background context if it is relevant…" + `[Lore]` block.
- If `context.use_template=true`: render template output as a system message; also include a small raw tail controlled by `context.keep_history_tail`.
- Budgeting: use `model.context_window` minus `model.max_tokens`; trim oldest history first; truncate the current user content last.

## Participation & conversation mode
- General chat only in `participation.general_chat.allowed_channels`.
- `participation.general_chat.response_chance_override`: channels that always pass the random chance gate (cooldowns still apply).
- Mentions/name aliases always allowed (style `reply`).
- Conversation mode: after a reply, `conversation_mode.enabled/window_seconds/max_messages`; batching loop drains per-channel buffers and posts one summary reply.

## Logging & debugging
- Set `LOG_LEVEL`: `INFO|DEBUG|FULL` (FULL also logs partial payloads; no secrets). Library verbosity via `LIB_LOG_LEVEL`.
- `LOG_PROMPTS=true` writes prompts to `logs/prompts-YYYYMMDD-HHMMSS/{prompt.json,prompt.txt,response.txt}` (may contain sensitive info).
- Common log lines: `decision …`, `llm-start/llm-finish …`, `tokenizer-summary …`, lore `lore-include` and `lore-limit-reached`.
- Correlation: `utils/correlation.make_correlation_id()` ties decision → LLM call → post.

## Run & env
- Windows/PowerShell run:
  ```powershell
  $env:PYTHONPATH = "src"
  python -m src.bot_app
  ```
- Secrets in `.env`: `DISCORD_TOKEN`, `OPENROUTER_API_KEY`.

## Editing conventions for agents
- Add config: put keys in `config.yaml` and expose minimal getters in `ConfigService`; keep defaults safe.
- Extend policy or routing: prefer small, explicit flags in config and keep decisions logged via `ParticipationPolicy._log_decision`.
- New providers: implement `LLMClient`-compatible class in `src/llm/`, wire in `bot_app.py`, and gate via config; do not remove OpenRouter.
- Be careful with logging: never log secrets; prefer structured fields using `fmt()`.
- Avoid changing public method signatures unless you update all call sites and the docs/comments nearby.
