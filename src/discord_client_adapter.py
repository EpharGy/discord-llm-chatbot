from __future__ import annotations

import discord
from discord import Intents


class DiscordClientAdapter(discord.Client):
    def __init__(self, router, intents_cfg: dict, logger):
        intents = Intents.default()
        if intents_cfg.get("message_content", False):
            intents.message_content = True
        intents.members = bool(intents_cfg.get("members", False))
        intents.presences = bool(intents_cfg.get("presences", False))
        super().__init__(intents=intents)
        self.router = router
        self.log = logger

    async def on_ready(self):
        if self.user is not None:
            self.log.info(f"Logged in as {self.user} (ID: {self.user.id})")
        else:
            self.log.info("Logged in (user not available yet)")

    async def on_message(self, message: discord.Message):
        # Ignore messages from ourselves
        if self.user is not None and message.author.id == self.user.id:
            return
        await self.router.handle_message(message)
