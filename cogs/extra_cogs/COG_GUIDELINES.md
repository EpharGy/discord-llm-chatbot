# Custom Cog Guidelines

This bot supports two kinds of cogs:

- Simple slash-command cogs that do not use the LLM (basic commands).
- LLM-integrated cogs that send a compact result to the LLM via our Tool → LLM bridge to produce persona-styled replies.

Use the sections below to choose the right pattern and keep behavior consistent.

## ✅ Simple slash-command cogs (no LLM)

Use this when you just need a straightforward command that returns a fixed or computed result without persona styling.

Key points:

- Put the file under `cogs/extra_cogs/` (e.g., `ping.py`).
- Define a `commands.Cog` subclass and an `async def setup(bot)` that adds the cog.
- If you’ll deny a request (permissions/validation), don’t defer first—reply immediately and ephemerally.
- If it’s a normal public reply and you need async work first, `defer(ephemeral=False)` and then `followup.send(...)`.
- Store/compare times in UTC; format for users in local time.

Minimal example:

```python
import discord
from discord.ext import commands
from discord import app_commands

class PingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="ping", description="Basic ping without LLM")
    async def ping(self, interaction: discord.Interaction):
        # For simple, fast replies you can skip defer and respond directly:
        await interaction.response.send_message("Pong!", ephemeral=False)

async def setup(bot):
    await bot.add_cog(PingCog(bot))
```

Ephemeral denial pattern (no defer):

```python
if not allowed:
    await interaction.response.send_message("You don't have permission.", ephemeral=True)
    return
```

## 🤖 LLM-integrated cogs (Tool → LLM)

Use this when you want persona-styled replies. Do your logic first (API calls, computation), then feed a compact result to the LLM via `ToolBridge`.

Core pattern:

- Compute the result (no LLM yet).
- Build `summary` (one clear line) and optional `details` (short lines with extra info).
- Call `ToolBridge(router).run(...)` with `tool_name`, `intent`, `summary`, `details`, and a `style_hint`.
- If the tool/router isn’t available or errors, fall back to your computed text.
- Split long messages to respect Discord’s 2k limit.

Minimal example:

```python
import discord
import logging
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timezone

try:
    from src.tool_bridge import ToolBridge
except Exception:
    from ...src.tool_bridge import ToolBridge

class ExampleCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.log = logging.getLogger(__name__)

    @app_commands.command(name="example", description="LLM-styled reply")
    async def example(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=False)

        # Your logic here (no LLM yet)
        value = 42
        summary = f"Computed value is {value}"
        details = f"computed_at_utc={datetime.now(timezone.utc).isoformat()}"

        router = getattr(self.bot, 'router', None)
        text = None
        if router is not None:
            is_nsfw = bool(getattr(interaction.channel, 'nsfw', False)) or bool(getattr(getattr(interaction.channel, 'parent', None), 'nsfw', False))
            try:
                text = await ToolBridge(router).run(
                    channel_id=str(getattr(interaction.channel, 'id', 'web-room')),
                    tool_name="example",
                    intent="show_value",
                    summary=summary,
                    details=details,
                    block_char_limit=1024,
                    is_nsfw=is_nsfw,
                    temperature=0.3,
                    style_hint="Respond in one concise sentence with a friendly tone. Do not ask questions.",
                )
            except Exception:
                text = None

        if not text:
            text = summary

        splitter = getattr(router, '_split_for_discord', None) if router else None
        parts = splitter(text) if callable(splitter) else [text]
        if not isinstance(parts, (list, tuple)):
            parts = [str(parts)]
        for part in parts:
            await interaction.followup.send(part, ephemeral=False)

async def setup(bot):
    await bot.add_cog(ExampleCog(bot))
```

Notes:

- Do not pass `reply_max_tokens`; we rely on model defaults from config.
- Keep `summary` lean; `details` gets truncated by `block_char_limit` if too long.
- If you need ephemeral replies (private), set `defer(ephemeral=True)` and keep followups ephemeral.

## 🧭 Common conventions

Imports with fallback (works with `PYTHONPATH=src`):

```python
try:
    from src.config_service import ConfigService
except Exception:
    from ...src.config_service import ConfigService
```

Time handling:

- Store/compare in UTC with tz: `datetime.now(timezone.utc)`.
- Display to users in local time: `dt.astimezone().strftime('%Y-%m-%d %H:%M')`.

Message splitting (Discord 2k limit):

```python
splitter = getattr(router, '_split_for_discord', None)
if callable(splitter):
    parts = splitter(text)
    if not isinstance(parts, (list, tuple)):
        parts = [str(parts)]
else:
    parts = [text[i:i+1900] for i in range(0, len(text), 1900)]
for part in parts:
    await interaction.followup.send(part, ephemeral=False)
```

Other guidelines:

- Permissions (optional): use `ConfigService("config.yaml")` and check `discord_admin_user_ids()` / `discord_elevated_user_ids()`.
- HTTP calls: prefer `httpx.AsyncClient` with short timeouts; handle non-200; never log secrets.
- NSFW prompt: compute `is_nsfw` from channel and pass to `ToolBridge.run`.
- Logging: `logging.getLogger(__name__)`; prompt dumps are managed centrally via `LOG_PROMPTS`.
- Files next to the cog: keep JSON config/cache in the same folder; handle file-not-found/parse errors.
- Errors: catch exceptions around network calls and ToolBridge; provide a plain-text fallback.

## ✅ Checklist

- [ ] File under `cogs/extra_cogs/`, plus `async def setup(bot)`.
- [ ] For simple commands: reply directly or defer+followup; no ToolBridge needed.
- [ ] For LLM replies: compute first, then ToolBridge; include `summary` and optional `details`.
- [ ] UTC for storage/comparison; local time for user-facing strings.
- [ ] Proper defer/response flow; unauthorized users get immediate ephemeral denial.
- [ ] Logging via `logging.getLogger(__name__)`; no secrets in logs.

That’s it—copy the minimal examples and adapt as needed. If you want per-tool model or style overrides, we can add a small `tools.*` config block later.
