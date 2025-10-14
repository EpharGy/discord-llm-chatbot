from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from src.config_service import ConfigService
from src.logger_factory import set_log_levels, get_logger
from src.participation_policy import ParticipationPolicy
import yaml as _pyyaml

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

    # --- YAML round-trip helpers (preserve comments when ruamel.yaml is available) ---
    @staticmethod
    def _yaml_rt():
        try:
            from ruamel.yaml import YAML  # type: ignore
            y = YAML()
            y.preserve_quotes = True
            y.indent(sequence=2, offset=2)
            return y
        except Exception:
            return None

    @classmethod
    def _read_config(cls, path: str):
        from pathlib import Path
        p = Path(path)
        if not p.exists():
            return {}
        y = cls._yaml_rt()
        with p.open("r", encoding="utf-8") as f:
            if y is not None:
                return y.load(f) or {}
            return _pyyaml.safe_load(f) or {}

    @classmethod
    def _write_config(cls, path: str, data: dict) -> None:
        from pathlib import Path
        p = Path(path)
        y = cls._yaml_rt()
        with p.open("w", encoding="utf-8") as f:
            if y is not None:
                y.dump(data, f)
            else:
                # Fallback: PyYAML (will drop comments)
                _pyyaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

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
        # Usage log
        try:
            uname = getattr(interaction.user, "display_name", None) or getattr(interaction.user, "name", "user")
            log.info(f"[Discord] {uname} used /llmbot_restart")
        except Exception:
            pass
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
        # Usage log
        try:
            uname = getattr(interaction.user, "display_name", None) or getattr(interaction.user, "name", "user")
            log.info(f"[Discord] {uname} used /llmbot_debug {level_up}")
        except Exception:
            pass
        if level_up not in ("INFO", "DEBUG", "FULL"):
            await interaction.response.send_message("Invalid level. Use INFO, DEBUG, or FULL.", ephemeral=True)
            return
        try:
            cfg = ConfigService("config.yaml")
            # Apply immediately
            set_log_levels(level=level_up, lib_log_level=cfg.lib_log_level())
            # Persist to config.yaml with comment preservation when possible
            data = self._read_config("config.yaml")
            data["LOG_LEVEL"] = level_up
            self._write_config("config.yaml", data)
            await interaction.response.send_message(f"Log level set to {level_up}.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Failed to set level: {e}", ephemeral=True)

    @app_commands.check(_admin_check)
    @app_commands.command(name="llmbot_logprompts", description="Toggle LOG_PROMPTS on/off")
    @app_commands.describe(enabled="true to enable, false to disable")
    async def llmbot_logprompts(self, interaction: discord.Interaction, enabled: bool):
        # Usage log
        try:
            uname = getattr(interaction.user, "display_name", None) or getattr(interaction.user, "name", "user")
            log.info(f"[Discord] {uname} used /llmbot_logprompts {enabled}")
        except Exception:
            pass
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        try:
            # Update config file value and acknowledge (preserve comments when possible)
            data = self._read_config("config.yaml")
            data["LOG_PROMPTS"] = bool(enabled)
            self._write_config("config.yaml", data)
            await interaction.followup.send(f"LOG_PROMPTS set to {enabled}.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Failed to update LOG_PROMPTS: {e}", ephemeral=True)

    # --- General chat channel management ---

    def _update_channel_flag(self, *, path: list[str], channel_id: int, enabled: bool) -> tuple[bool, list[str]]:
        data = self._read_config("config.yaml")
        node = data
        for key in path[:-1]:
            if key not in node or not isinstance(node[key], dict):
                node[key] = {}
            node = node[key]
        leaf = path[-1]
        raw_list = node.get(leaf)
        if not isinstance(raw_list, list):
            raw_list = []
        raw_list = [str(x) for x in raw_list]
        chan = str(channel_id)
        existing = list(raw_list)
        changed = False
        if enabled:
            if chan not in existing:
                raw_list.append(chan)
                changed = True
        else:
            new_list = [c for c in raw_list if c != chan]
            if len(new_list) != len(raw_list):
                changed = True
            raw_list = new_list
        node[leaf] = raw_list
        if changed:
            self._write_config("config.yaml", data)
        return changed, raw_list

    def _reload_participation_policy(self) -> bool:
        router = getattr(self.bot, "router", None)
        if router is None:
            return False
        try:
            cfg = ConfigService("config.yaml")
            new_policy = ParticipationPolicy(cfg.rate_limits(), cfg.participation())
            try:
                new_policy.set_window_size(cfg.window_size())
            except Exception:
                pass
            router.policy = new_policy
            return True
        except Exception as exc:
            log.error(f"policy-reload-failed {exc}")
            return False

    async def _handle_general_toggle(self, interaction: discord.Interaction, *, enabled: bool, override: bool = False) -> None:
        channel = interaction.channel
        if channel is None:
            await interaction.response.send_message("This command must be used in a channel context.", ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        path = ["participation", "general_chat", "response_chance_override" if override else "allowed_channels"]
        changed, values = self._update_channel_flag(path=path, channel_id=channel.id, enabled=enabled)
        status = "enabled" if enabled else "disabled"
        scope = "override" if override else "general chat"
        mention = f"<#{channel.id}>"
        try:
            uname = getattr(interaction.user, "display_name", None) or getattr(interaction.user, "name", "user")
            log.info(
                f"[Discord] {uname} used /{'llmbot_general_override' if override else 'llmbot_general'} {enabled} in {channel.id}"
            )
        except Exception:
            pass
        if changed:
            applied = self._reload_participation_policy()
            note = "Config reloaded." if applied else "Update saved; reload may be required."
            await interaction.followup.send(
                f"{scope.title()} {status} for {mention}. (Current entries: {len(values)})\n{note}",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"No changes made. {mention} was already {status if enabled else 'removed'} for {scope}.",
                ephemeral=True,
            )

    @app_commands.check(_admin_check)
    @app_commands.command(name="llmbot_general", description="Enable or disable general chat participation in this channel")
    @app_commands.describe(enabled="true to allow general chat responses, false to remove")
    async def llmbot_general(self, interaction: discord.Interaction, enabled: bool):
        await self._handle_general_toggle(interaction, enabled=enabled, override=False)

    @app_commands.check(_admin_check)
    @app_commands.command(name="llmbot_general_override", description="Force 100% general chat response chance in this channel")
    @app_commands.describe(enabled="true to force response chance override, false to remove")
    async def llmbot_general_override(self, interaction: discord.Interaction, enabled: bool):
        await self._handle_general_toggle(interaction, enabled=enabled, override=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
