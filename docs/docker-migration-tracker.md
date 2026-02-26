# Docker Migration Tracker (Validation Phase)

Purpose: single forward-looking tracker for Docker migration on NAS while we validate stability before calling migration complete.

## Current phase

**Status:** validating

We have a working lift-and-shift container path. This document now tracks only:
- required completed baseline items
- remaining validation and hardening work

## Completed baseline (required)

- Root-level Docker deployment is in place and running:
  - [Dockerfile](../Dockerfile)
  - [docker-compose.yml](../docker-compose.yml)
  - [.dockerignore](../.dockerignore)
- Runtime supports Docker-friendly persistence/path overrides where needed:
  - [cogs/reminders.py](../cogs/reminders.py) (`REMINDER_FILE`)
  - [cogs/extra_cogs/price_tracker.py](../cogs/extra_cogs/price_tracker.py) (`PRICE_TRACKER_DB_FILE`, `PRICE_TRACKER_IMAGE_DIR`)
- Main app entrypoint remains the same for local and container runs:
  - [src/bot_app.py](../src/bot_app.py)

## Validation goals (in progress)

Migration is considered validated when all items below are complete:

1. **Stability window**
	- Run at least 7 continuous days without critical restart loops/crashes.

2. **Persistence verification**
	- Confirm reminders and price-tracker data survive container restarts/recreates.
	- Confirm web room data and logs persist as expected.

3. **Feature parity on NAS**
	- Validate Discord behavior (mentions, replies, conversation mode).
	- Validate Web behavior (`/health`, UI chat, `/reset`, optional bearer token).

4. **Operational recovery**
	- Verify backup/restore workflow for `./cogs`, `./logs`, and `./config.yaml`.

## Next steps (priority order)

1. Complete and record 7-day validation observations (uptime + error patterns).
2. Run a controlled restart/recreate test and capture persistence results.
3. Run a backup/restore drill and confirm bot resumes normally.
4. Decide whether to keep single-container `BOTH` mode as default long-term.

## Post-validation hardening backlog

Do after validation passes (not blocking current phase):

- Centralize router/provider wiring to reduce drift risk between startup paths.
- Replace broad startup/background exception swallowing with summary logging.
- Add optional fail-fast mode for missing required runtime files in Docker.
- Keep config documentation aligned with `ConfigService` behavior.

## Operating notes

- Keep writable data on host mounts (`./cogs`, `./logs`) and mount `./config.yaml` writable.
- Keep secrets outside the image (`.env`/host environment).
- Prefer env-based storage path overrides over hardcoded write paths.
