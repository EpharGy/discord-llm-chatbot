# Docker Migration Tracker

Purpose: keep Docker migration work easy to audit, so temporary MVP artifacts can be removed or promoted cleanly.

## Status legend
- `KEEP`: intended long-term
- `TEMP`: MVP-only helper, remove after full migration is stable
- `PROMOTE`: currently MVP, likely to become permanent with minor edits

## Current inventory

### Runtime code
- `KEEP` [src/bot_app.py](src/bot_app.py)
- `TEMP` [docker-bot/src/simple_ping_bot.py](docker-bot/src/simple_ping_bot.py) — Discord connectivity smoke test only
- `KEEP` [cogs/reminders.py](cogs/reminders.py) — now supports `REMINDER_FILE` override
- `KEEP` [cogs/extra_cogs/price_tracker.py](cogs/extra_cogs/price_tracker.py) — now supports `PRICE_TRACKER_DB_FILE` and `PRICE_TRACKER_IMAGE_DIR`

### Container files
- `PROMOTE` [docker-bot/Dockerfile](docker-bot/Dockerfile) — currently ping default command; usable for full bot by command override
- `TEMP` [docker-bot/docker-compose.yml](docker-bot/docker-compose.yml) — MVP compose profile
- `KEEP` [docker-bot/.dockerignore](docker-bot/.dockerignore)
- `KEEP` [docker-bot/.env.example](docker-bot/.env.example)

### Docs
- `KEEP` [docs/nas-docker-plan.md](docs/nas-docker-plan.md)
- `TEMP` [docker-bot/README.md](docker-bot/README.md)

## Exit criteria for removing TEMP files

Remove `TEMP` artifacts only after all are true:
1. Full bot runs in container for at least 7 days without critical restart loops.
2. Reminder and price-tracker data persist correctly across restarts.
3. Discord command and scheduled-task behavior validated on NAS.
4. Backup/restore of `./data` confirmed.

## Cleanup plan

When stable:
1. Switch compose command to full bot (`python -m src.bot_app`).
2. Keep `docker-bot/docker-compose.yml` for one more release cycle as rollback.
3. If no rollback needed, remove:
   - [docker-bot/src/simple_ping_bot.py](docker-bot/src/simple_ping_bot.py)
   - [docker-bot/README.md](docker-bot/README.md)
4. Rename `docker-bot/docker-compose.yml` to a standard production filename.

## Notes
- Keep writable data isolated in host-mounted folders (`./data`, `./logs`).
- Prefer env-based path overrides over hardcoded in-repo write paths.
