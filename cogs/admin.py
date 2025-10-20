from __future__ import annotations

import asyncio
import discord
from discord import app_commands
from discord.ext import commands

from src.config_service import ConfigService
from src.logger_factory import set_log_levels, get_logger
from src.participation_policy import ParticipationPolicy
from src.llm.openrouter_catalog import get_catalog, refresh_catalog_with_logging, ModelInfo
from src.utils.time_utils import now_local
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
            # Refresh OpenRouter catalog as part of restart
            try:
                refresh_catalog_with_logging(log, context="/llmbot_restart")
            except Exception:
                pass
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

    # --- Model management helpers ---
    def _hot_apply_model_cfg(self) -> bool:
        router = getattr(self.bot, "router", None)
        if router is None:
            return False
        try:
            cfg = ConfigService("config.yaml")
            router.model_cfg = cfg.model()
            return True
        except Exception as exc:
            log.error(f"model-hot-apply-failed {exc}")
            return False

    def _format_price(self, v: float | None) -> str:
        try:
            if v is None:
                return "n/a"
            # OpenRouter pricing appears to be USD per token in cache; scale to per-million for display
            per_million = float(v) * 1_000_000.0
            return f"${per_million:.2f}/M"
        except Exception:
            return "n/a"

    def _format_ctx(self, v: int | str | None) -> str:
        try:
            if v is None:
                return "n/a"
            n = int(v)
            if n >= 1_000_000:
                return f"{int(round(n/1_000_000))}M"
            if n >= 1_000:
                return f"{int(round(n/1_000))}K"
            return str(n)
        except Exception:
            return "n/a"

    # Note: intentionally no admin check here so non-admins can view the list.
    @app_commands.command(name="llmbot_model_order", description="Show configured models (admins can move one to top)")
    @app_commands.choices(scope=[
        app_commands.Choice(name="normal", value="normal"),
        app_commands.Choice(name="vision", value="vision"),
    ])
    async def llmbot_model_order(self, interaction: discord.Interaction, scope: app_commands.Choice[str]):
        # Usage log
        try:
            uname = getattr(interaction.user, "display_name", None) or getattr(interaction.user, "name", "user")
            log.info(f"[Discord] {uname} used /llmbot_model_order scope={scope.value}")
        except Exception:
            pass
        try:
            await interaction.response.defer(ephemeral=False)
        except Exception:
            pass
        cfg = ConfigService("config.yaml")
        scope_key = scope.value
        if scope_key == "vision":
            try:
                models = ((cfg.model().get("openrouter") or {}).get("vision") or {}).get("models") or []
            except Exception:
                models = []
        else:
            models = (cfg.model().get("openrouter") or {}).get("models") or []
        if isinstance(models, str):
            models = [m.strip() for m in models.split(",") if m.strip()]
        models = [str(m) for m in models]
        cat = get_catalog()
        lines = []
        for i, slug in enumerate(models, start=1):
            info = cat.get(slug)
            ctx_raw = info.context_length if info and info.context_length else None
            ctx = self._format_ctx(ctx_raw)
            pp = self._format_price(info.prompt_per_million if info else None)
            cp = self._format_price(info.completion_per_million if info else None)
            # vision=True means supports image input (not image generation)
            vis = " 📷" if (info and info.vision) else ""
            hint = slug.split("/")[-1]
            lines.append(f"[{i}] {hint} — {slug} — ctx {ctx}, prompt {pp}, completion {cp}{vis}")
        if not lines:
            lines.append("(No models configured)")
        is_admin = _is_admin(interaction.user)
        if is_admin:
            lines.append("")
            if scope_key == "vision":
                lines.append("Reply to this message with the Model # (within 45s) to move it to position 1 in the VISION list.")
            else:
                lines.append("Reply to this message with the Model # (within 45s) to move it to position 1 in the NORMAL list.")
        content = "\n".join(lines)
        # Send and capture the message for reply checking
        out_msg = None
        try:
            out_msg = await interaction.followup.send(content, ephemeral=False)
        except Exception:
            try:
                out_msg = await interaction.followup.send(content)
            except Exception:
                return
        # For non-admins, we're done after showing the list
        if not is_admin:
            return
        # Admin path: wait for a reply from the same user in the same channel
        try:
            def check(m: discord.Message):
                if m.author.id != interaction.user.id:
                    return False
                # Require a message reply to our bot message
                ref = getattr(m, 'reference', None)
                replied_id = getattr(ref, 'message_id', None) if ref else None
                if replied_id is None and ref and getattr(ref, 'cached_message', None):
                    try:
                        replied_id = ref.cached_message.id
                    except Exception:
                        replied_id = None
                if not out_msg or replied_id != getattr(out_msg, 'id', None):
                    return False
                return True
            msg = await self.bot.wait_for("message", timeout=45.0, check=check)
            choice_raw = (msg.content or "").strip()
            try:
                idx = int(choice_raw)
            except Exception:
                await interaction.followup.send("Not a number. Use /llmbot_model_add to add a new model by id.", ephemeral=True)
                return
            if idx < 1 or idx > len(models):
                await interaction.followup.send("Index out of range.", ephemeral=True)
                return
            sel = models[idx - 1]
            new_list = [sel] + [m for m in models if m != sel]
            data = self._read_config("config.yaml")
            node = data.setdefault("model", {})
            ornode = node.setdefault("openrouter", {})
            if scope_key == "vision":
                v = ornode.setdefault("vision", {})
                v["models"] = new_list
            else:
                ornode["models"] = new_list
            self._write_config("config.yaml", data)
            self._hot_apply_model_cfg()
            try:
                uname = getattr(interaction.user, "display_name", None) or getattr(interaction.user, "name", "user")
                log.info(f"[models-reorder] user={uname} scope={scope_key} moved_to_top={sel}")
            except Exception:
                pass
            await interaction.followup.send(f"Moved to top: {sel}", ephemeral=True)
        except asyncio.TimeoutError:
            try:
                await interaction.followup.send("Timed out. Run /llmbot_model_order again when ready.", ephemeral=True)
            except Exception:
                pass

        
    @app_commands.check(_admin_check)
    @app_commands.command(name="llmbot_model_add", description="Search OpenRouter catalog and add a model to top of rotation")
    @app_commands.choices(scope=[
        app_commands.Choice(name="normal", value="normal"),
        app_commands.Choice(name="vision", value="vision"),
    ])
    @app_commands.describe(query="Space-separated search terms matched against full model id (AND search)")
    async def llmbot_model_add(self, interaction: discord.Interaction, scope: app_commands.Choice[str], query: str):
        # Usage log
        try:
            uname = getattr(interaction.user, "display_name", None) or getattr(interaction.user, "name", "user")
            log.info(f"[Discord] {uname} used /llmbot_model_add scope={scope.value} query=\"{(query or '').strip()}\"")
        except Exception:
            pass
        # Non-ephemeral so the admin can send a number as a normal message in-channel
        try:
            await interaction.response.defer(ephemeral=False)
        except Exception:
            pass
        terms = [t.strip().lower() for t in (query or "").split() if t.strip()]
        if not terms:
            await interaction.followup.send("Provide at least one search term.")
            return
        cat = get_catalog()
        models_map = cat.list()
        scope_key = scope.value
        # Build candidate list with optional vision pre-filter
        cand = []
        for slug, info in models_map.items():
            if scope_key == "vision" and not info.vision:
                continue
            s = slug.lower()
            if all(t in s for t in terms):
                cand.append((slug, info))
        if not cand:
            await interaction.followup.send("No matches.")
            return
        # Sort: newest release first, then cheaper prompt price, then completion, then slug
        def _price_key(mi: ModelInfo | None):
            p = mi.prompt_per_million if mi else None
            c = mi.completion_per_million if mi else None
            # Treat None as very large to push to the end
            return (
                1e12 if p is None else float(p),
                1e12 if c is None else float(c),
            )
        def _release_key(mi: ModelInfo | None):
            r = mi.released_at if mi else None
            return -(r if isinstance(r, (int, float)) else -1)  # None -> bottom
        cand.sort(key=lambda x: (_release_key(x[1]), _price_key(x[1]), x[0]))
        # Prepare output with a safe cap for Discord 2k limit
        lines = []
        max_items = 20
        for i, (slug, info) in enumerate(cand[:max_items], start=1):
            ctx_raw = info.context_length if info and info.context_length else None
            ctx = self._format_ctx(ctx_raw)
            pp = self._format_price(info.prompt_per_million if info else None)
            cp = self._format_price(info.completion_per_million if info else None)
            vis = " 📷" if (info and info.vision) else ""
            hint = slug.split("/")[-1]
            lines.append(f"[{i}] {hint} — {slug} — ctx {ctx}, prompt {pp}, completion {cp}{vis}")
        if len(cand) > max_items:
            lines.append(f"… and {len(cand) - max_items} more")
        lines.append("")
        lines.append("Reply to this message with the Model # (within 45s) to add it to the top.")
        out = "\n".join(lines)
        # Send and capture message for reply checking
        add_msg = None
        try:
            add_msg = await interaction.followup.send(out)
        except Exception:
            # If too long, trim to first 10 and resend
            lines = lines[:12]
            add_msg = await interaction.followup.send("\n".join(lines))
        # Wait for admin reply with index
        try:
            def check(m: discord.Message):
                if m.author.id != interaction.user.id:
                    return False
                # Require reply to our add message
                ref = getattr(m, 'reference', None)
                replied_id = getattr(ref, 'message_id', None) if ref else None
                if replied_id is None and ref and getattr(ref, 'cached_message', None):
                    try:
                        replied_id = ref.cached_message.id
                    except Exception:
                        replied_id = None
                if not add_msg or replied_id != getattr(add_msg, 'id', None):
                    return False
                return True
            msg = await self.bot.wait_for("message", timeout=45.0, check=check)
            choice_raw = (msg.content or "").strip()
            try:
                idx = int(choice_raw)
            except Exception:
                await interaction.followup.send("Not a number. Aborted.")
                return
            if idx < 1 or idx > min(max_items, len(cand)):
                await interaction.followup.send("Index out of range.")
                return
            slug = cand[idx - 1][0]
            data = self._read_config("config.yaml")
            node = data.setdefault("model", {})
            ornode = node.setdefault("openrouter", {})
            if scope_key == "vision":
                v = ornode.setdefault("vision", {})
                current = v.get("models") or []
                if isinstance(current, str):
                    current = [m.strip() for m in current.split(",") if m.strip()]
                current = [str(m) for m in current]
                new_list = [slug] + [m for m in current if m != slug]
                v["models"] = new_list
            else:
                current = ornode.get("models") or []
                if isinstance(current, str):
                    current = [m.strip() for m in current.split(",") if m.strip()]
                current = [str(m) for m in current]
                new_list = [slug] + [m for m in current if m != slug]
                ornode["models"] = new_list
            self._write_config("config.yaml", data)
            self._hot_apply_model_cfg()
            try:
                uname = getattr(interaction.user, "display_name", None) or getattr(interaction.user, "name", "user")
                log.info(f"[models-add] user={uname} scope={scope_key} added_to_top={slug}")
            except Exception:
                pass
            await interaction.followup.send(f"Added to top: {slug}")
        except asyncio.TimeoutError:
            try:
                await interaction.followup.send("Timed out. Run the command again when ready.")
            except Exception:
                pass   

async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
