from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import yaml


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


@dataclass
class Persona:
    meta: dict
    body: str


class PersonaService:
    def __init__(self, path: str):
        self.path = Path(path) if path else Path("personas/default.md")
        self._persona = self._load()
        try:
            self._mtime_ns = self.path.stat().st_mtime_ns
        except Exception:
            self._mtime_ns = 0

    def set_path(self, path: str) -> None:
        """Update persona file path and reload immediately."""
        p = Path(path)
        if p != self.path:
            self.path = p
            self._persona = self._load()
            try:
                self._mtime_ns = self.path.stat().st_mtime_ns
            except Exception:
                self._mtime_ns = 0

    def _load(self) -> Persona:
        try:
            text = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        except Exception:
            text = ""
        m = _FRONTMATTER_RE.match(text)
        if m:
            fm, body = m.group(1), m.group(2)
            meta = yaml.safe_load(fm) or {}
        else:
            meta, body = {}, text
        return Persona(meta=meta, body=body.strip())

    def _maybe_reload(self) -> None:
        try:
            m = self.path.stat().st_mtime_ns
        except Exception:
            return
        if m != getattr(self, "_mtime_ns", 0):
            try:
                self._persona = self._load()
                self._mtime_ns = m
            except Exception:
                pass

    def meta(self) -> dict:
        self._maybe_reload()
        return self._persona.meta

    def body(self) -> str:
        self._maybe_reload()
        return self._persona.body
