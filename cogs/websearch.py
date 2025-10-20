from __future__ import annotations

import discord
from discord.ext import commands
from discord import app_commands

from src.config_service import ConfigService
from src.utils.time_utils import now_local
from src.logger_factory import get_logger

log = get_logger("Cog.WebSearch")


class WebSearchCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="websearch", description="Ask the bot to search the web using web models and return an answer")
    async def websearch(self, interaction: discord.Interaction, query: str):
        # Defer immediately (non-ephemeral result so others can see)
        try:
            await interaction.response.defer(ephemeral=False)
        except Exception:
            pass
        cfg = ConfigService("config.yaml")
        m = cfg.model() or {}
        ornode = (m.get("openrouter") or {}) if isinstance(m, dict) else {}
        web_cfg = (ornode.get("web") or {}) if isinstance(ornode, dict) else {}
        if not bool(web_cfg.get("enabled", False)):
            await interaction.followup.send("Web search is not enabled in config (model.openrouter.web.enabled=false).", ephemeral=True)
            return
        web_models = web_cfg.get("models") or []
        if isinstance(web_models, str):
            web_models = [s.strip() for s in web_models.split(",") if s.strip()]
        web_models = [str(s) for s in web_models if str(s).strip()]
        if not web_models:
            await interaction.followup.send("No web models configured (model.openrouter.web.models is empty).", ephemeral=True)
            return
        channel = getattr(interaction, 'channel', None)
        cid = str(getattr(channel, 'id', 'web-room'))
        # Step 1: Query web-enabled model(s) with only the user input, using OpenRouter web_search
        router = getattr(self.bot, 'router', None)
        if router is None or not getattr(router, 'llm', None):
            await interaction.followup.send("Router unavailable.")
            return
        corr = f"{cid}-websearch-{int(now_local().timestamp()*1000)}"
        provider_index = 0
        try:
            cf_probe = {"channel": cid, "user": "search", "correlation": corr, "web": True}
            if hasattr(router.llm, 'providers_for_context'):
                plist = router.llm.providers_for_context(cf_probe)
                if isinstance(plist, list):
                    for i, p in enumerate(plist):
                        if p.__class__.__name__ == 'OpenRouterClient':
                            provider_index = i
                            break
        except Exception:
            provider_index = 0
        web_text = None
        for slug in web_models:
            try:
                result = await router.llm.generate_chat(
                    messages=[{"role": "user", "content": query}],
                    max_tokens=cfg.response_tokens_max(),
                    model=slug,
                    temperature=router.model_cfg.get("temperature"),
                    top_p=router.model_cfg.get("top_p"),
                    stop=router.model_cfg.get("stop"),
                    context_fields={"channel": cid, "user": "search", "correlation": corr, "web": True, "provider_index": provider_index},
                )
                web_text = (result or {}).get('text') if isinstance(result, dict) else result
                if isinstance(web_text, str) and web_text.strip():
                    break
            except Exception as e:
                log.error(f"[websearch-step1-error] model={slug} err={e}")
                continue
        if not web_text:
            await interaction.followup.send("No web results.")
            return
        # Step 2: Build a normal reply using persona/lore/history and injecting the web results
        author_name = getattr(interaction.user, 'display_name', None) or getattr(interaction.user, 'name', 'user')
        ev = {
            'channel_id': cid,
            'channel_name': getattr(channel, 'name', cid),
            'author_name': author_name,
            'content': query,
            'web_context': f"User requested a web search to improve accuracy for: {query}\n\nResults:\n{web_text}\n\nResponse Guidelines: At the end of your response, cite at least 1 url used as a source using Markdown formatting for URLs.",
        }
        try:
            reply = await router.build_batch_reply(cid=cid, events=[ev], channel=channel, allow_outside_window=True)
            if not reply:
                await interaction.followup.send("No reply.")
                return
            parts = []
            splitter = getattr(router, '_split_for_discord', None)
            if callable(splitter):
                try:
                    parts = splitter(reply)
                    if not isinstance(parts, (list, tuple)):
                        parts = [str(parts)]
                except Exception:
                    parts = []
            if not parts:
                parts = [reply]
            for p in parts:
                await interaction.followup.send(p)
        except Exception as e:
            await interaction.followup.send(f"Error: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(WebSearchCog(bot))
