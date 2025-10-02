from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import yaml


@dataclass
class Config:
    raw: dict


class ConfigService:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        with self._path.open("r", encoding="utf-8") as f:
            self._cfg = Config(raw=yaml.safe_load(f) or {})
        try:
            self._mtime_ns = self._path.stat().st_mtime_ns
        except Exception:
            self._mtime_ns = 0

    def _maybe_reload(self) -> None:
        try:
            m = self._path.stat().st_mtime_ns
        except Exception:
            return
        if m != getattr(self, "_mtime_ns", 0):
            try:
                with self._path.open("r", encoding="utf-8") as f:
                    self._cfg = Config(raw=yaml.safe_load(f) or {})
                self._mtime_ns = m
            except Exception:
                # On read error, keep previous config
                pass

    def model(self) -> dict:
        return self._cfg.raw.get("model", {})

    def rate_limits(self) -> dict:
        return self._cfg.raw.get("rate_limits", {})

    def participation(self) -> dict:
        return self._cfg.raw.get("participation", {})

    def context(self) -> dict:
        return self._cfg.raw.get("context", {})

    def persona_path(self) -> str:
        self._maybe_reload()
        return str(self.context().get("persona_path", "personas/default.md"))

    def system_prompt_path(self) -> str:
        self._maybe_reload()
        return str(self.context().get("system_prompt_path", "prompts/system.txt"))

    def system_prompt_path_nsfw(self) -> str | None:
        self._maybe_reload()
        v = self.context().get("system_prompt_path_nsfw")
        return str(v) if v else None

    def context_template_path(self) -> str:
        self._maybe_reload()
        return str(self.context().get("context_template_path", "prompts/context_template.txt"))


    def discord_intents(self) -> dict:
        return self._cfg.raw.get("discord", {}).get("intents", {})

    def discord_admin_user_ids(self) -> set[str]:
        """Return admin user IDs from config as strings.

        Config path: discord.admin_user_ids: ["123", "456"]
        Accepts strings or numbers; normalizes to strings.
        """
        self._maybe_reload()
        ids = self._cfg.raw.get("discord", {}).get("admin_user_ids", [])
        out: set[str] = set()
        if isinstance(ids, (list, tuple)):
            for v in ids:
                try:
                    out.add(str(v))
                except Exception:
                    continue
        elif ids:
            out.add(str(ids))
        return out

    def discord_message_char_limit(self) -> int:
        self._maybe_reload()
        try:
            return int(self._cfg.raw.get("discord", {}).get("message_char_limit", 2000))
        except Exception:
            return 2000

    def max_response_messages(self) -> int:
        self._maybe_reload()
        try:
            return int(self._cfg.raw.get("discord", {}).get("max_response_messages", 2))
        except Exception:
            return 2

    def window_size(self) -> int:
        return int(self.context().get("window_size", 10))

    def use_template(self) -> bool:
        self._maybe_reload()
        return bool(self.context().get("use_template", True))

    def keep_history_tail(self) -> int:
        self._maybe_reload()
        return int(self.context().get("keep_history_tail", 2))

    def recency_minutes(self) -> int:
        self._maybe_reload()
        try:
            return int(self.context().get("recency_minutes", 10))
        except Exception:
            return 10

    def cluster_max_messages(self) -> int:
        """Maximum number of recent messages to keep in the conversation cluster."""
        self._maybe_reload()
        try:
            return int(self.context().get("cluster_max_messages", self.window_size()))
        except Exception:
            return self.window_size()

    def thread_affinity_max(self) -> int:
        """Max additional thread-affinity turns (bot or current author) to force-include."""
        self._maybe_reload()
        try:
            return int(self.context().get("thread_affinity_max", 6))
        except Exception:
            return 6

    # Lore configuration
    def lore_enabled(self) -> bool:
        self._maybe_reload()
        lore = self.context().get("lore", {})
        return bool(lore.get("enabled", False))

    def lore_paths(self) -> list[str]:
        self._maybe_reload()
        lore = self.context().get("lore", {})
        paths = lore.get("paths", [])
        if isinstance(paths, str):
            return [paths]
        return [str(p) for p in (paths or [])]

    def lore_max_fraction(self) -> float:
        self._maybe_reload()
        lore = self.context().get("lore", {})
        try:
            return float(lore.get("max_fraction", 0.33))
        except Exception:
            return 0.33

    def lore_md_priority(self) -> str:
        self._maybe_reload()
        lore = self.context().get("lore", {})
        v = str(lore.get("md_priority", "low")).lower()
        return "high" if v == "high" else "low"

    def log_level(self) -> str:
        return str(self._cfg.raw.get("LOG_LEVEL", "INFO")).upper()

    def lib_log_level(self) -> str | None:
        v = self._cfg.raw.get("LIB_LOG_LEVEL") or self._cfg.raw.get("LIV_LOG_LEVEL")
        return str(v).upper() if v else None

    def log_prompts(self) -> bool:
        self._maybe_reload()
        v = self._cfg.raw.get("LOG_PROMPTS", False)
        return bool(v)

    def log_console(self) -> bool:
        """Return whether to write logs to console (stdout). Uses LOG_CONSOLE only."""
        self._maybe_reload()
        return bool(self._cfg.raw.get("LOG_CONSOLE", True))

    def log_errors(self) -> bool:
        """Return whether to always write ERROR-and-above to logs/errors.log.

        This is independent of LOG_LEVEL and LOG_CONSOLE.
        """
        self._maybe_reload()
        return bool(self._cfg.raw.get("LOG_ERRORS", False))

    # Bot run mode (Discord/Web/Both)
    def bot_method(self) -> str:
        self._maybe_reload()
        v = str(self._cfg.raw.get("bot_type", {}).get("method", "DISCORD")).upper()
        if v not in ("DISCORD", "WEB", "BOTH"):
            return "DISCORD"
        return v

    def html_port(self) -> int:
        self._maybe_reload()
        try:
            return int((self._cfg.raw.get("http") or {}).get("html_port", 8005))
        except Exception:
            return 8005

    def html_host(self) -> str:
        """Host interface for the HTTP server. Default to 127.0.0.1 (safe)."""
        self._maybe_reload()
        v = (self._cfg.raw.get("http") or {}).get("html_host")
        return str(v) if v else "127.0.0.1"

    def http_auth_bearer_token(self) -> str | None:
        """Optional bearer token for HTTP endpoints; when set, required on protected routes."""
        self._maybe_reload()
        v = (self._cfg.raw.get("http") or {}).get("bearer_token")
        v = str(v).strip() if v else None
        return v or None

    def max_context_tokens(self) -> int:
        """Total model context window (tokens) for prompt + completion)."""
        self._maybe_reload()
        m = self.model()
        return int(m.get("context_window", m.get("context_window_tokens", 8192)))

    def response_tokens_max(self) -> int:
        """Reserved tokens for the model's completion (aka headroom). Uses model.max_tokens."""
        self._maybe_reload()
        return int(self.model().get("max_tokens", 512))

    def conversation_batch_interval_seconds(self) -> int:
        self._maybe_reload()
        return int(self.participation().get("conversation_mode", {}).get("batch_interval_seconds", 10))

    def conversation_batch_limit(self) -> int:
        self._maybe_reload()
        return int(self.participation().get("conversation_mode", {}).get("batch_limit", 10))

    # Vision (multimodal) configuration
    def vision(self) -> dict:
        self._maybe_reload()
        return self._cfg.raw.get("vision", {})

    def vision_enabled(self) -> bool:
        v = self.vision()
        return bool(v.get("enabled", False))

    def vision_max_images(self) -> int:
        v = self.vision()
        try:
            return int(v.get("max_images", 4))
        except Exception:
            return 4

    def vision_models(self) -> list[str]:
        v = self.vision()
        ms = v.get("models", [])
        if isinstance(ms, str):
            return [ms]
        return [str(m) for m in (ms or [])]

    def vision_mode(self) -> str:
        v = self.vision()
        m = str(v.get("mode", "single-pass")).lower()
        return "two-pass" if m == "two-pass" else "single-pass"

    def vision_retry_on_count_error(self) -> bool:
        v = self.vision()
        return bool(v.get("retry_on_image_count_error", True))

    def vision_fallback_to_text(self) -> bool:
        v = self.vision()
        return bool(v.get("fallback_to_text", True))

    def vision_apply_in(self) -> dict:
        v = self.vision()
        apply = v.get("apply_in", {}) or {}
        return {
            "mentions": bool(apply.get("mentions", True)),
            "replies": bool(apply.get("replies", True)),
            "general_chat": bool(apply.get("general_chat", False)),
            "batch": bool(apply.get("batch", False)),
        }

    def vision_log_image_urls(self) -> bool:
        v = self.vision()
        return bool(v.get("log_image_urls", False))

    def vision_timeout_multiplier(self) -> float:
        v = self.vision()
        try:
            return float(v.get("timeout_multiplier", 1.5))
        except Exception:
            return 1.5
