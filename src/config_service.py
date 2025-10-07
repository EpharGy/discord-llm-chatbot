from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
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
    # Persona config cache
        self._persona_name_cache: str | None = None
        self._persona_yaml_path: Path | None = None
        self._persona_yaml_mtime: int = 0
        self._persona_cfg_cache: dict | None = None

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
        self._maybe_reload()
        base = self._cfg.raw.get("participation", {}) or {}
        # Merge/override with persona-provided name aliases; fallback to default persona when missing
        out = dict(base)
        pc = self._persona_cfg() or {}
        aliases = pc.get("name_aliases") or (pc.get("participation", {}) or {}).get("name_aliases")
        if not aliases:
            dpc = self._persona_cfg_for("default") or {}
            aliases = dpc.get("name_aliases") or (dpc.get("participation", {}) or {}).get("name_aliases")
        if isinstance(aliases, (list, tuple)):
            try:
                out["name_aliases"] = [str(a) for a in aliases if str(a).strip()]
            except Exception:
                pass
        return out

    def context(self) -> dict:
        return self._cfg.raw.get("context", {})

    # ---------- Persona folder-based configuration ----------
    def persona_name(self) -> str:
        """Return the selected persona folder name (top-level 'persona'), default 'default'.

        Only allows safe folder names (alnum, dash, underscore). Falls back to 'default'.
        """
        self._maybe_reload()
        name = self._cfg.raw.get("persona") or self.context().get("persona")
        if not isinstance(name, str) or not name.strip():
            name = "default"
        name = name.strip()
        if not re.match(r"^[A-Za-z0-9_-]+$", name):
            name = "default"
        return name

    def _persona_root(self) -> Path:
        return Path("personas") / self.persona_name()

    def _persona_root_for(self, name: str) -> Path:
        return Path("personas") / name

    def _persona_yaml_candidates(self) -> list[Path]:
        root = self._persona_root()
        return [root / "default.yaml", root / f"{self.persona_name()}.yaml"]

    def _persona_yaml_candidates_for(self, name: str) -> list[Path]:
        root = self._persona_root_for(name)
        return [root / "default.yaml", root / f"{name}.yaml"]

    def _persona_cfg(self) -> dict:
        """Load persona YAML config from personas/<name>/default.yaml (or <name>.yaml).

        Caches and reloads when the YAML file changes. Returns {} if missing.
        Expected keys (relative to persona root unless absolute):
          - system_prompt
          - system_prompt_nsfw
          - context_template
          - persona_file
          - name_aliases
          - lore.paths: [list]
        """
        try:
            # Determine current yaml path
            ypath = None
            for p in self._persona_yaml_candidates():
                if p.exists():
                    ypath = p
                    break
            if ypath is None:
                self._persona_yaml_path = None
                self._persona_yaml_mtime = 0
                self._persona_cfg_cache = {}
                return {}
            st = ypath.stat().st_mtime_ns
            if self._persona_yaml_path == ypath and self._persona_yaml_mtime == st and self._persona_cfg_cache is not None:
                return self._persona_cfg_cache
            # Reload
            with ypath.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                data = {}
            self._persona_yaml_path = ypath
            self._persona_yaml_mtime = st
            self._persona_cfg_cache = data
            return data
        except Exception:
            self._persona_cfg_cache = {}
            return {}

    def _persona_cfg_for(self, name: str) -> dict:
        try:
            for p in self._persona_yaml_candidates_for(name):
                if p.exists():
                    with p.open("r", encoding="utf-8") as f:
                        d = yaml.safe_load(f) or {}
                        return d if isinstance(d, dict) else {}
            return {}
        except Exception:
            return {}

    def _resolve_rel(self, path_val: str | None, root: Path) -> Path | None:
        if not path_val or not isinstance(path_val, str) or not path_val.strip():
            return None
        p = Path(path_val)
        if not p.is_absolute():
            p = root / path_val
        return p

    def persona_path(self) -> str:
        """Return path to the persona markdown file, derived from persona YAML or sensible defaults.

        Resolution order:
          1) personas/<name>/<persona_file> if specified in persona YAML
          2) personas/<name>/persona.md if exists
          3) personas/<name>.md if exists
          4) personas/default.md (legacy fallback)
        """
        self._maybe_reload()
        root = self._persona_root()
        pc = self._persona_cfg()
        # 1) From YAML
        # Support either 'persona_file' or 'persona' in YAML
        rel = None
        if isinstance(pc, dict):
            rel = pc.get("persona_file") or pc.get("persona")
        if isinstance(rel, str) and rel.strip():
            p = self._resolve_rel(rel, root)
            if p is not None and p.exists():
                return str(p)
            # Fallback to default persona file if selected one missing
            dpc = self._persona_cfg_for("default")
            droot = self._persona_root_for("default")
            drel = (dpc.get("persona_file") or dpc.get("persona")) if isinstance(dpc, dict) else None
            dp = self._resolve_rel(drel, droot)
            if dp is not None and dp.exists():
                return str(dp)
        # 2) Conventional defaults
        for cand in [root / "persona.md", Path("personas") / f"{self.persona_name()}.md", Path("personas") / "default.md"]:
            try:
                if cand.exists():
                    return str(cand)
            except Exception:
                continue
        return str(root / "persona.md")

    def system_prompt_path(self) -> str:
        self._maybe_reload()
        pc = self._persona_cfg()
        root = self._persona_root()
        v = pc.get("system_prompt") if isinstance(pc, dict) else None
        # Use persona value if exists
        p = self._resolve_rel(v, root)
        if p is not None and p.exists():
            return str(p)
        # Fallback to default persona
        dpc = self._persona_cfg_for("default")
        droot = self._persona_root_for("default")
        dv = dpc.get("system_prompt") if isinstance(dpc, dict) else None
        dp = self._resolve_rel(dv, droot)
        if dp is not None and dp.exists():
            return str(dp)
        # Finally, hard fallback under default persona folder
        return str((self._persona_root_for("default") / "system.txt"))

    def system_prompt_path_nsfw(self) -> str | None:
        self._maybe_reload()
        pc = self._persona_cfg()
        root = self._persona_root()
        v = pc.get("system_prompt_nsfw") if isinstance(pc, dict) else None
        p = self._resolve_rel(v, root)
        if p is not None and p.exists():
            return str(p)
        # Fallback default persona nsfw
        dpc = self._persona_cfg_for("default")
        droot = self._persona_root_for("default")
        dv = dpc.get("system_prompt_nsfw") if isinstance(dpc, dict) else None
        dp = self._resolve_rel(dv, droot)
        if dp is not None and dp.exists():
            return str(dp)
        # Finally, hard fallback under default persona folder if exists
        dpp = self._persona_root_for("default") / "system_nsfw.txt"
        try:
            return str(dpp) if dpp.exists() else None
        except Exception:
            return None

    def context_template_path(self) -> str:
        self._maybe_reload()
        pc = self._persona_cfg()
        root = self._persona_root()
        v = pc.get("context_template") if isinstance(pc, dict) else None
        p = self._resolve_rel(v, root)
        if p is not None and p.exists():
            return str(p)
        # Fallback default persona template
        dpc = self._persona_cfg_for("default")
        droot = self._persona_root_for("default")
        dv = dpc.get("context_template") if isinstance(dpc, dict) else None
        dp = self._resolve_rel(dv, droot)
        if dp is not None and dp.exists():
            return str(dp)
        # Finally, hard fallback under default persona folder
        return str((self._persona_root_for("default") / "context_template.txt"))


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

    def discord_elevated_user_ids(self) -> set[str]:
        """Return elevated user IDs from config as strings.

        Config path: discord.elevated_user_ids: ["123", "456"].
        Accepts strings or numbers; normalizes to strings.
        """
        self._maybe_reload()
        ids = self._cfg.raw.get("discord", {}).get("elevated_user_ids", [])
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
        """Merge global lore paths with persona-provided lore paths (if any).

        Persona YAML may include:
          lore:
            paths: ["relative/to/persona", "/abs/path.md"]
        These are resolved relative to the persona root when not absolute.
        """
        self._maybe_reload()
        # Global
        lore = self.context().get("lore", {}) or {}
        paths = lore.get("paths", [])
        if isinstance(paths, str):
            glob_list = [paths]
        else:
            glob_list = [str(p) for p in (paths or [])]
        # Persona
        pc = self._persona_cfg()
        add_list: list[str] = []
        try:
            plore = []
            if isinstance(pc, dict):
                lv = pc.get("lore")
                if isinstance(lv, dict):
                    plore = lv.get("paths", []) or []
                elif isinstance(lv, list):
                    plore = lv
            if isinstance(plore, str):
                plore = [plore]
            root = self._persona_root()
            for v in (plore or []):
                try:
                    p = Path(v)
                    if not p.is_absolute():
                        p = root / v
                    if p.exists():
                        add_list.append(str(p))
                except Exception:
                    continue
        except Exception:
            pass
        return list(dict.fromkeys(glob_list + add_list))

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

    def http_message_limit(self) -> int:
        self._maybe_reload()
        try:
            return int((self._cfg.raw.get("http") or {}).get("message_limit", 200))
        except Exception:
            return 200

    def http_inactive_room_days(self) -> float:
        self._maybe_reload()
        try:
            return float((self._cfg.raw.get("http") or {}).get("inactive_room_days", 1))
        except Exception:
            return 1.0

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

    # Vision (multimodal) configuration (now under model.vision; keep legacy fallback)
    def vision(self) -> dict:
        self._maybe_reload()
        m = self.model() or {}
        v = m.get("vision") or {}
        if not v:
            # Legacy fallback: top-level "vision" for backward compatibility
            v = self._cfg.raw.get("vision", {}) or {}
        return v

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

    # ---------- Diagnostics ----------
    def persona_diagnostics(self) -> list[str]:
        """Return a list of human-readable warnings about missing persona assets.

        Checks only files explicitly referenced by the selected persona YAML. If a referenced
        file is missing, a warning is emitted indicating that default persona fallbacks will be used.
        Also warns if default persona fallbacks are missing.
        """
        warnings: list[str] = []
        try:
            name = self.persona_name()
            root = self._persona_root()
            pc = self._persona_cfg() or {}
            # Helper to check a rel path exists under a root
            def _check(label: str, rel: str | None, r: Path, is_optional: bool = False):
                if not rel or not isinstance(rel, str) or not rel.strip():
                    return
                p = self._resolve_rel(rel, r)
                try:
                    if p is None or not p.exists():
                        warnings.append(f"persona '{name}': missing {label} -> {rel} (root={r}), will fall back to default persona")
                except Exception:
                    warnings.append(f"persona '{name}': unable to access {label} -> {rel} (root={r})")

            # Check referenced files in selected persona
            _check("system_prompt", pc.get("system_prompt"), root)
            _check("system_prompt_nsfw", pc.get("system_prompt_nsfw"), root, is_optional=True)
            _check("context_template", pc.get("context_template"), root, is_optional=True)
            _check("persona file", (pc.get("persona_file") or pc.get("persona")), root)
            # Lore list (accept list or lore.paths)
            lore_list = []
            lv = pc.get("lore")
            if isinstance(lv, dict):
                lore_list = lv.get("paths", []) or []
            elif isinstance(lv, list):
                lore_list = lv
            if isinstance(lore_list, str):
                lore_list = [lore_list]
            for rel in (lore_list or []):
                p = self._resolve_rel(rel, root)
                try:
                    if p is None or not p.exists():
                        warnings.append(f"persona '{name}': lore missing -> {rel} (root={root})")
                except Exception:
                    warnings.append(f"persona '{name}': unable to access lore -> {rel} (root={root})")

            # Validate default persona fallbacks exist when needed
            droot = self._persona_root_for("default")
            dpc = self._persona_cfg_for("default") or {}
            def _exists(rel: str | None, r: Path) -> bool:
                p = self._resolve_rel(rel, r)
                try:
                    return bool(p and p.exists())
                except Exception:
                    return False
            # If selected persona points to something missing, ensure default has it
            def _ensure_default(label: str, key: str, hard_fallback: str | None = None):
                rel = pc.get(key)
                if isinstance(rel, str) and rel.strip():
                    p = self._resolve_rel(rel, root)
                    missing = not (p and p.exists())
                    if missing:
                        drel = dpc.get(key)
                        if not _exists(drel, droot):
                            # Try hard fallback file under default folder if provided
                            if hard_fallback:
                                hf = droot / hard_fallback
                                if not hf.exists():
                                    warnings.append(f"default persona: missing {label} fallback -> {hard_fallback}")
                            else:
                                warnings.append(f"default persona: missing {label} referenced in YAML")
            _ensure_default("system_prompt", "system_prompt", hard_fallback="system.txt")
            _ensure_default("system_prompt_nsfw", "system_prompt_nsfw", hard_fallback="system_nsfw.txt")
            _ensure_default("context_template", "context_template", hard_fallback="context_template.txt")
            # Persona file fallback
            prel = (pc.get("persona_file") or pc.get("persona"))
            if isinstance(prel, str) and prel.strip():
                pp = self._resolve_rel(prel, root)
                if not (pp and pp.exists()):
                    # Check default persona file
                    dprel = (dpc.get("persona_file") or dpc.get("persona"))
                    dpp = self._resolve_rel(dprel, droot)
                    if not (dpp and dpp.exists()):
                        # Check conventional default.md
                        if not (droot / "persona.md").exists() and not (Path("personas") / "default.md").exists():
                            warnings.append("default persona: missing persona markdown (persona.md or default.md)")
        except Exception:
            # Diagnostics should never crash startup
            pass
        return warnings
