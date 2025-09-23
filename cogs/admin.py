from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from src.config_service import ConfigService
from src.logger_factory import set_log_levels, get_logger

log = get_logger("Cog.Admin")


def _is_admin(user: discord.abc.User | discord.Member) -> bool:
    try:
        cfg = ConfigService("config.yaml")
        admins = cfg.discord_admin_user_ids()
        return str(getattr(user, "id", "")) in admins
    except Exception:
        return False


async def _admin_check(interaction: discord.Interaction) -> bool:
    if _is_admin(interaction.user):
        return True
    raise app_commands.CheckFailure("Not authorized.")


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            try:
                if interaction.response.is_done():
                    await interaction.followup.send("Not authorized.", ephemeral=True)
                else:
                    await interaction.response.send_message("Not authorized.", ephemeral=True)
            except Exception:
                pass
            return
        # Fallback logging
        try:
            await interaction.response.send_message(f"Error: {error}", ephemeral=True)
        except Exception:
            try:
                await interaction.followup.send(f"Error: {error}", ephemeral=True)
            except Exception:
                pass

    @app_commands.check(_admin_check)
    @app_commands.command(name="llmbot_restart", description="Reload config.yaml and hot-apply settings")
    async def llmbot_restart(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        # Trigger a reload via ConfigService and apply key toggles
        try:
            cfg = ConfigService("config.yaml")
            # Apply log levels dynamically
            level = cfg.log_level()
            lib = cfg.lib_log_level()
            set_log_levels(level=level, lib_log_level=lib)
            await interaction.followup.send("Config reloaded and logging updated.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Reload failed: {e}", ephemeral=True)

    @app_commands.check(_admin_check)
    @app_commands.command(name="llmbot_debug", description="Set LOG_LEVEL (INFO | DEBUG | FULL)")
    @app_commands.describe(level="Desired log level")
    async def llmbot_debug(self, interaction: discord.Interaction, level: str):
        level_up = (level or "").upper()
        if level_up not in ("INFO", "DEBUG", "FULL"):
            await interaction.response.send_message("Invalid level. Use INFO, DEBUG, or FULL.", ephemeral=True)
            return
        try:
            cfg = ConfigService("config.yaml")
            # Apply immediately
            set_log_levels(level=level_up, lib_log_level=cfg.lib_log_level())
            # Persist to config.yaml
            from pathlib import Path
            import yaml
            p = Path("config.yaml")
            data = {}
            if p.exists():
                with p.open("r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
            data["LOG_LEVEL"] = level_up
            with p.open("w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
            await interaction.response.send_message(f"Log level set to {level_up} (persisted).", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Failed to set level: {e}", ephemeral=True)

    @app_commands.check(_admin_check)
    @app_commands.command(name="llmbot_logprompts", description="Toggle LOG_PROMPTS on/off")
    @app_commands.describe(enabled="true to enable, false to disable")
    async def llmbot_logprompts(self, interaction: discord.Interaction, enabled: bool):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        try:
            # Update config file value and acknowledge
            from pathlib import Path
            import yaml
            p = Path("config.yaml")
            data = {}
            if p.exists():
                with p.open("r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
            data["LOG_PROMPTS"] = bool(enabled)
            with p.open("w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
            await interaction.followup.send(f"LOG_PROMPTS set to {enabled}.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Failed to update LOG_PROMPTS: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
