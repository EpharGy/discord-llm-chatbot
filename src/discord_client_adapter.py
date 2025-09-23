from __future__ import annotations

import asyncio
import importlib
import importlib.util
import pkgutil
from pathlib import Path
import discord
from discord import Intents
from discord.ext import commands


class DiscordClientAdapter(commands.Bot):
    def __init__(self, router, intents_cfg: dict, logger):
        intents = Intents.default()
        if intents_cfg.get("message_content", False):
            intents.message_content = True
        intents.members = bool(intents_cfg.get("members", False))
        intents.presences = bool(intents_cfg.get("presences", False))
        super().__init__(command_prefix=commands.when_mentioned_or("!"), intents=intents)
        self.router = router
        self.log = logger

    async def setup_hook(self) -> None:
        # Load cogs from cogs/ and cogs/extra_cogs/
        found_cogs = 0
        loaded_cogs = 0
        for base in (Path("src/cogs"), Path("src/cogs/extra_cogs")):
            if not base.exists():
                continue
            # Discover python modules in the directory
            for module_info in pkgutil.iter_modules([str(base)]):
                name = module_info.name
                if name.startswith("_"):
                    continue
                found_cogs += 1
                mod_path = f"src.cogs.{name}" if base.name == "cogs" else f"src.cogs.extra_cogs.{name}"
                try:
                    spec = importlib.util.spec_from_file_location(mod_path, str(base / f"{name}.py"))
                    if spec and spec.loader:
                        module = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(module)
                        if hasattr(module, "setup"):
                            await module.setup(self)
                            loaded_cogs += 1
                except Exception as e:
                    self.log.error(f"Failed to load cog {mod_path}: {e}")
        self.log.info(f"cogs-loaded found={found_cogs} loaded={loaded_cogs}")

        # Sync app commands (slash commands)
        try:
            synced = await self.tree.sync()
            total = len(synced) if synced is not None else len(self.tree.get_commands())
            self.log.info(f"slash-commands-synced count={total}")
        except Exception as e:
            self.log.error(f"Slash command sync failed: {e}")

    async def on_ready(self):
        if self.user is not None:
            self.log.info(f"Logged in as {self.user} (ID: {self.user.id})")
        else:
            self.log.info("Logged in (user not available yet)")

    async def on_message(self, message: discord.Message):
        # Allow text commands/cogs to process if needed
        await self.process_commands(message)
        # Ignore messages from ourselves
        if self.user is not None and message.author.id == self.user.id:
            return
        await self.router.handle_message(message)
