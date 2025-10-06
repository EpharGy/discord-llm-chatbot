from __future__ import annotations

from jinja2 import Environment, BaseLoader
from pathlib import Path
from .persona_service import PersonaService


class PromptTemplateEngine:
    def __init__(self, system_prompt_path: str, context_template_path: str, persona_service: PersonaService):
        self._system_path = system_prompt_path or ""
        self._context_path = context_template_path or ""
        sp = Path(self._system_path) if self._system_path else None
        cp = Path(self._context_path) if self._context_path else None
        self.system_prompt = (sp.read_text(encoding="utf-8") if (sp and sp.exists()) else "")
        self.context_template = (cp.read_text(encoding="utf-8") if (cp and cp.exists()) else "")
        self._sys_mtime_ns = (sp.stat().st_mtime_ns if (sp and sp.exists()) else 0)
        self._ctx_mtime_ns = (cp.stat().st_mtime_ns if (cp and cp.exists()) else 0)
        self.persona = persona_service
        self.env = Environment(loader=BaseLoader())

    def _maybe_reload_templates(self):
        sp = Path(self._system_path) if self._system_path else None
        cp = Path(self._context_path) if self._context_path else None
        try:
            sm = (sp.stat().st_mtime_ns if (sp and sp.exists()) else 0)
            if sm != getattr(self, "_sys_mtime_ns", 0):
                self.system_prompt = (sp.read_text(encoding="utf-8") if (sp and sp.exists()) else "")
                self._sys_mtime_ns = sm
        except Exception:
            pass
        try:
            cm = (cp.stat().st_mtime_ns if (cp and cp.exists()) else 0)
            if cm != getattr(self, "_ctx_mtime_ns", 0):
                self.context_template = (cp.read_text(encoding="utf-8") if (cp and cp.exists()) else "")
                self._ctx_mtime_ns = cm
        except Exception:
            pass

    def _hot_reload_config_paths(self):
        # Hot-apply template path changes from config without restart (base system + context)
        try:
            from .config_service import ConfigService
            cfg = ConfigService("config.yaml")
            new_system = cfg.system_prompt_path()
            new_context = cfg.context_template_path()
            if new_system and str(new_system) != str(getattr(self, "_system_path", "")):
                self._system_path = str(new_system)
                self._sys_mtime_ns = -1
                self._maybe_reload_templates()
            if new_context and str(new_context) != str(getattr(self, "_context_path", "")):
                self._context_path = str(new_context)
                self._ctx_mtime_ns = -1
                self._maybe_reload_templates()
        except Exception:
            pass

    def _maybe_apply_nsfw_override(self, is_nsfw: bool) -> None:
        # Honor participation.allow_nsfw toggle; when false, never switch to NSFW system prompt
        try:
            from .config_service import ConfigService
            cfg = ConfigService("config.yaml")
            allow_nsfw = bool(cfg.participation().get("allow_nsfw", True))
        except Exception:
            allow_nsfw = True
        if not is_nsfw or not allow_nsfw:
            return
        try:
            from .config_service import ConfigService
            cfg = ConfigService("config.yaml")
            nsfw_path = cfg.system_prompt_path_nsfw()
            if nsfw_path and str(nsfw_path) != str(getattr(self, "_system_path", "")):
                p = Path(nsfw_path)
                if p.exists():
                    self._system_path = str(nsfw_path)
                    self._sys_mtime_ns = -1
                    self._maybe_reload_templates()
                    from .logger_factory import get_logger
                    get_logger("PromptTemplate").debug(f"[prompt-select] nsfw=true path={nsfw_path}")
        except Exception:
            pass

    def build_system_message_for(self, is_nsfw: bool = False) -> str:
        # Reload templates if files changed
        self._maybe_reload_templates()
        # Hot reload base paths
        self._hot_reload_config_paths()
        # Apply NSFW override if needed
        self._maybe_apply_nsfw_override(is_nsfw=is_nsfw)
        # Hot-apply persona path changes from config without restart
        try:
            from .config_service import ConfigService
            cfg = ConfigService("config.yaml")
            current_path = cfg.persona_path()
            # Compare as strings to avoid Path normalization differences
            if str(getattr(self.persona, "path", "")) != str(current_path):
                self.persona.set_path(current_path)
        except Exception:
            # If config load fails, keep existing persona path
            pass
        persona_block = self.persona.body()
        return f"{self.system_prompt}\n\n[Persona]\n{persona_block}"

    # Backwards compatibility: existing callers
    def build_system_message(self) -> str:  # pragma: no cover - thin wrapper
        return self.build_system_message_for(is_nsfw=False)

    def render(self, conversation_window, user_input: str, summary: str | None = None) -> str:
        # Build a context block with buckets: last user message, recent (<= recency window), older (> window)
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        try:
            from .config_service import ConfigService
            cfg = ConfigService("config.yaml")
            recency = int(cfg.context().get("recency_minutes", 10))
        except Exception:
            recency = 10
        cutoff = now - timedelta(minutes=recency)

        last_user = None
        for m in reversed(conversation_window):
            if m.get("role") == "user":
                last_user = m
                break
        if not last_user:
            last_user = {"timestamp_iso": now.isoformat(), "author": "user", "content": user_input, "role": "user"}

        recent_messages = []
        older_messages = []
        for m in conversation_window:
            ts = m.get("timestamp_iso")
            try:
                dt = datetime.fromisoformat(ts) if ts else None
            except Exception:
                dt = None
            (recent_messages if (dt and dt >= cutoff) else older_messages).append(m)

        tmpl = self.env.from_string(self.context_template or "{{''}}")
        ctx = {
            "recent_messages": recent_messages,
            "older_messages": older_messages,
            "last_user": last_user,
            "user_input": user_input,
            "summary": summary or "",
        }
        context_block = tmpl.render(**ctx)
        # Important: Do NOT include the system/persona here; MessageRouter already adds the
        # base system message separately. Returning only the context block avoids duplication
        # in outgoing prompts and keeps configuration behavior consistent.
        return context_block or ""
