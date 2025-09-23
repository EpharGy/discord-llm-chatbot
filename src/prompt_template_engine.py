from __future__ import annotations

from jinja2 import Environment, BaseLoader
from pathlib import Path
from .persona_service import PersonaService
from .config_service import ConfigService


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

    def build_system_message(self) -> str:
        # Reload templates if files changed
        self._maybe_reload_templates()
        persona_block = self.persona.body()
        return f"{self.system_prompt}\n\n[Persona]\n{persona_block}"

    def render(self, conversation_window, user_input: str, summary: str | None = None) -> str:
        # Build a context block with buckets: last user message, recent (<=10m), older (>10m)
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        # Allow config-driven recency window
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
            bucket = recent_messages if (dt and dt >= cutoff) else older_messages
            bucket.append(m)

        tmpl = self.env.from_string(self.context_template or "{{''}}")
        ctx = {
            "recent_messages": recent_messages,
            "older_messages": older_messages,
            "last_user": last_user,
            "user_input": user_input,
            "summary": summary or "",
        }
        context_block = tmpl.render(**ctx)
        return f"{self.build_system_message()}\n\n{context_block}" if context_block else self.build_system_message()
