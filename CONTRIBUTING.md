# Contributing

Thanks for your interest in contributing! This project welcomes PRs.

## Quick start

1. Fork and clone the repo.
2. Create and activate a virtual environment.
3. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

4. Copy templates if needed (first run does this automatically): `.env.example -> .env`, `config.example.yaml -> config.yaml`.
5. Run the bot locally:

   ```powershell
   $env:PYTHONPATH = "src"
   python -m src.bot_app
   ```

## Coding standards

- Python 3.10+
- Keep changes small and focused.
- Avoid leaking secrets in logs. Use the structured logging helpers.
- Prefer config-driven behavior; add minimal getters to `ConfigService`.

## Lint & format

- Keep code readable and small; no mandatory linter or test runner is enforced by this repo.

## Tests

- Add or update tests for externally visible behavior.
- Keep unit tests fast and isolated from network calls.

## Pull requests

- Describe the problem and the solution.
- Include screenshots or logs when helpful.
- Reference related issues.

Thanks again!
