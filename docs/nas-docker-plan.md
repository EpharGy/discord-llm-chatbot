# Synology DS920+ Docker Migration Plan (MVP First)

This plan is intentionally small and beginner-friendly. Goal: prove the bot runs reliably in Docker on Synology before adding complexity.

## Why Docker here
- Synology package Python may be below the app minimum (`>=3.10`).
- Your project already supports running in `WEB`, `DISCORD`, or `BOTH`, so we can test in safe stages.

---

## Phase 0 (MVP): Web-only smoke test

Start with **one container** and **web mode only** (`bot_type.method: WEB`).
This avoids Discord token and gateway variables while validating config, dependencies, static assets, model calls, and persistence.

### Minimal `Dockerfile`
```dockerfile
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY cogs/ ./cogs/
COPY prompts/ ./prompts/
COPY personas/ ./personas/
COPY lore/ ./lore/
COPY config.example.yaml ./config.example.yaml

CMD ["python", "-m", "src.bot_app"]
```

### Minimal `docker-compose.yml` (single service)
```yaml
services:
  bot:
    build: .
    image: discord-llm-bot:latest
    env_file: .env
    volumes:
      - ./config.yaml:/app/config.yaml
      - ./logs:/app/logs
      - ./src/web/data:/app/src/web/data
    ports:
      - "8005:8005"
    restart: unless-stopped
```

### Required config for this phase
- `bot_type.method: WEB`
- `http.html_host: 0.0.0.0`
- `http.html_port: 8005`
- Optional: `http.bearer_token` (recommended if exposed beyond LAN)

### Smoke test checklist (must pass before next phase)
1. `docker compose up -d --build`
2. `GET /health` returns `{"ok": true}`
3. Open `http://<NAS-IP>:8005/`
4. Send one chat message and receive a model response
5. Restart container and confirm logs + room data persist

If this fails, do not add Discord yet.

### Alternative MVP (even smaller): Discord Ping bot

If you want to validate NAS + Discord first (without LLM/web complexity), use:
- `docker-bot/Dockerfile`
- `docker-bot/docker-compose.yml`
- `docker-bot/src/simple_ping_bot.py`
- `docker-bot/README.md`

This runs a minimal bot where `Ping!` returns `Pong!` and keeps security defaults (non-root, no published ports, read-only filesystem).

For future reminder/price-tracker persistence, the code now supports env-based storage overrides:
- `REMINDER_FILE`
- `PRICE_TRACKER_DB_FILE`
- `PRICE_TRACKER_IMAGE_DIR`

Use these with a writable host mount (for example `./data:/app/data`) while keeping container root read-only.

---

## Phase 1: Add Discord in same container

After Phase 0 is stable:
- Set `bot_type.method: BOTH`
- Add `DISCORD_TOKEN` to `.env`
- Keep same compose service/volume layout

Validation:
- Web still works (`/health`, UI chat)
- Bot logs in to Discord and responds
- No CPU/memory instability on NAS under light traffic

---

## Phase 2: Optional split services

Only split into separate `bot` and `web` services if needed for scaling or process isolation.
For first deployment, one service is simpler and easier to debug.

---

## Synology-specific notes

- DS920+ is x86_64, compatible with `python:3.11-slim`.
- Keep secrets external (`.env`, Synology env UI), not baked into image.
- Mount logs and room data as volumes.
- Prefer LAN-only exposure first; later put web behind Synology reverse proxy + TLS.

---

## Code review findings that impact Docker reliability

These are good cleanup targets after MVP works:

1. **Duplicate router/provider wiring** in both startup paths:
   - `src/bot_app.py`
   - `src/http_app.py` (`build_router_from_config`)

   Recommendation: centralize app wiring in one factory to avoid drift bugs.

2. **Very broad `except Exception: pass` usage** in startup/background loops (`src/bot_app.py`).

   Recommendation: keep best-effort behavior, but log exception summaries so container logs show failures.

3. **Auto-creating `.env` and `config.yaml` at startup** (`src/bot_app.py`) can mask misconfiguration in containers.

   Recommendation: in Docker mode, fail fast if required files are missing (or gate auto-create behind a config flag).

4. **Config docs vs runtime drift risk** (README and comments must match `ConfigService` behavior).

   Recommendation: keep one canonical config reference and validate keys on startup.

---

## “Small feature” validation idea (after smoke test)

Implement exactly one low-risk enhancement to prove your Docker workflow:
- Add `/ready` endpoint that checks:
  - config file loaded
  - model provider configured
  - writable logs/data paths

This gives a concrete health signal for Synology and reverse-proxy health checks without changing bot behavior.

---

## Suggested execution order

1. Create Dockerfile + one-service compose
  - For isolated MVP use only files under `docker-bot/`
2. Set config to `WEB`
3. Pass smoke tests
4. Switch to `BOTH` and validate Discord
5. Then do cleanup refactors (factory + exception logging)
