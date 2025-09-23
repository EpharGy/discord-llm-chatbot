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
5. Run a quick smoke test:

   ```powershell
   $env:PYTHONPATH = "src"
   python -m pytest -q
   ```

## Coding standards

- Python 3.10+
- Keep changes small and focused.
- Avoid leaking secrets in logs. Use the structured logging helpers.
- Prefer config-driven behavior; add minimal getters to `ConfigService`.

## Lint & format

- We use Ruff for linting/formatting in CI. You can install locally:

  ```powershell
  pip install ruff ruff-lsp
  ruff check .
  ruff format .
  ```

## Tests

- Add or update tests for externally visible behavior.
- Keep unit tests fast and isolated from network calls.

## Pull requests

- Describe the problem and the solution.
- Include screenshots or logs when helpful.
- Reference related issues.

Thanks again!
