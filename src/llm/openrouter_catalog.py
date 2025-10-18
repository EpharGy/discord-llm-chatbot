from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional
import json
import os
import httpx
from datetime import datetime, timezone


@dataclass
class ModelInfo:
    slug: str
    prompt_per_million: Optional[float] = None
    completion_per_million: Optional[float] = None
    context_length: Optional[int] = None
    vision: bool = False
    released_at: Optional[float] = None  # epoch seconds (newer = larger)


class OpenRouterCatalog:
    """Fetches and caches OpenRouter model metadata (pricing, context, vision).

    Cache is stored as JSON so we can rehydrate without network on restart.
    """

    def __init__(self, cache_path: Path | str = Path("logs/cache/openrouter_models.json")):
        self.cache_path = Path(cache_path)
        self.models: Dict[str, ModelInfo] = {}

    def _ensure_cache_dir(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    def load_cache(self) -> bool:
        try:
            if self.cache_path.exists():
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
                out: Dict[str, ModelInfo] = {}
                for k, v in (data or {}).items():
                    out[k] = ModelInfo(
                        slug=k,
                        prompt_per_million=v.get("prompt_per_million"),
                        completion_per_million=v.get("completion_per_million"),
                        context_length=v.get("context_length"),
                        vision=bool(v.get("vision", False)),
                        released_at=v.get("released_at"),
                    )
                self.models = out
                return True
        except Exception:
            pass
        return False

    def save_cache(self) -> None:
        try:
            self._ensure_cache_dir()
            data = {
                k: {
                    "prompt_per_million": v.prompt_per_million,
                    "completion_per_million": v.completion_per_million,
                    "context_length": v.context_length,
                    "vision": v.vision,
                    "released_at": v.released_at,
                }
                for k, v in self.models.items()
            }
            self.cache_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def refresh_from_network(self, timeout: float = 15.0) -> bool:
        """Fetch from OpenRouter models endpoint and update cache.

        Returns True on success, False on failure (cache kept).
        """
        url = "https://openrouter.ai/api/v1/models"
        headers = {}
        api_key = os.getenv("OPENROUTER_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            with httpx.Client(timeout=timeout) as client:
                r = client.get(url, headers=headers)
                r.raise_for_status()
                payload = r.json() or {}
        except Exception:
            return False
        try:
            items = payload.get("data") or payload.get("models") or []
            out: Dict[str, ModelInfo] = {}
            for m in items:
                slug = m.get("id") or m.get("slug") or m.get("name")
                if not slug:
                    continue
                ctx = m.get("context_length") or m.get("context_length_input") or m.get("max_context_length")
                pr = m.get("pricing") or {}
                # OpenRouter typically returns per-million costs
                prompt_per_million = None
                completion_per_million = None
                try:
                    # price may be nested like { prompt: {usd: 15.0}, completion: {usd: 30.0} }
                    p_val = pr.get("prompt")
                    if isinstance(p_val, dict):
                        usd = p_val.get("usd")
                        if isinstance(usd, (int, float, str)):
                            prompt_per_million = float(usd)
                    elif isinstance(p_val, (int, float, str)):
                        prompt_per_million = float(p_val)
                except Exception:
                    pass
                try:
                    c_val = pr.get("completion")
                    if isinstance(c_val, dict):
                        usd = c_val.get("usd")
                        if isinstance(usd, (int, float, str)):
                            completion_per_million = float(usd)
                    elif isinstance(c_val, (int, float, str)):
                        completion_per_million = float(c_val)
                except Exception:
                    pass
                # Vision (image input) support heuristic
                vision = False
                # 1) capabilities flags
                caps = m.get("capabilities") or {}
                if isinstance(caps, dict):
                    if any(bool(caps.get(k)) for k in ("vision", "image", "multimodal", "image_input")):
                        vision = True
                # 2) architecture.modality (e.g., "text", "image", "text+image")
                if not vision:
                    arch = m.get("architecture") or {}
                    if isinstance(arch, dict):
                        modality = str(arch.get("modality") or arch.get("modality_type") or "").lower()
                        if any(x in modality for x in ("image", "text+image", "multimodal")):
                            vision = True
                # 3) input_modalities array (e.g., ["text","image"])
                if not vision:
                    imods = m.get("input_modalities") or m.get("modalities")
                    if isinstance(imods, list):
                        vision = any(str(x).lower() == "image" or "image" in str(x).lower() for x in imods)
                # 4) tags fallback
                if not vision:
                    tags = m.get("tags") or []
                    if isinstance(tags, list):
                        vision = any("vision" in str(t).lower() or "multimodal" in str(t).lower() for t in tags)
                # Release date parsing (best-effort)
                def _parse_ts(val) -> Optional[float]:
                    try:
                        if val is None:
                            return None
                        if isinstance(val, (int, float)):
                            # some APIs may return epoch seconds or ms; normalize if it looks like ms
                            v = float(val)
                            if v > 10_000_000_000:  # > ~year 2286 if seconds; assume ms
                                v = v / 1000.0
                            return v
                        if isinstance(val, str):
                            s = val.strip()
                            # handle 'Z'
                            if s.endswith('Z'):
                                s = s[:-1] + '+00:00'
                            try:
                                dt = datetime.fromisoformat(s)
                            except Exception:
                                # Try date-only
                                try:
                                    dt = datetime.strptime(val, "%Y-%m-%d")
                                except Exception:
                                    return None
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            return dt.timestamp()
                    except Exception:
                        return None
                    return None
                # Choose the most relevant timestamp: released/created/updated/last_updated
                rel = (
                    _parse_ts(m.get("released_at"))
                    or _parse_ts(m.get("release_date"))
                    or _parse_ts(m.get("created"))
                    or _parse_ts(m.get("created_at"))
                    or _parse_ts(m.get("updated_at"))
                    or _parse_ts(m.get("last_updated"))
                    or _parse_ts((m.get("top_provider") or {}).get("last_updated"))
                    or _parse_ts((m.get("top_provider") or {}).get("created"))
                )
                out[str(slug)] = ModelInfo(
                    slug=str(slug),
                    prompt_per_million=prompt_per_million,
                    completion_per_million=completion_per_million,
                    context_length=(int(ctx) if ctx else None),
                    vision=bool(vision),
                    released_at=rel,
                )
            if out:
                self.models = out
                self.save_cache()
                return True
        except Exception:
            return False
        return False

    def get(self, slug: str) -> Optional[ModelInfo]:
        return self.models.get(slug)

    def list(self) -> Dict[str, ModelInfo]:
        return dict(self.models)


# Singleton-style accessor
_CATALOG: Optional[OpenRouterCatalog] = None


def get_catalog() -> OpenRouterCatalog:
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = OpenRouterCatalog()
        # Best-effort hydrate from cache
        try:
            _CATALOG.load_cache()
        except Exception:
            pass
    return _CATALOG


def refresh_catalog_with_logging(logger=None, *, context: str = "startup") -> bool:
    """Refresh the catalog and log a consistent message.

    - logger: any object with .info/.warning (e.g., our repo logger). If None, no logs.
    - context: short string to indicate callsite (e.g., "startup", "/llmbot_restart").
    Returns True on network refresh success, False otherwise.
    """
    cat = get_catalog()
    try:
        ok = bool(cat.refresh_from_network())
        if logger is not None:
            if ok:
                logger.info(f"openrouter-catalog: fetched and cached ({context})")
            else:
                logger.warning(f"openrouter-catalog: network refresh failed ({context}); using cached (if any)")
        return ok
    except Exception as e:
        if logger is not None:
            logger.warning(f"openrouter-catalog: refresh exception ({context}): {e}")
        return False


# Back-compat shim used by older callsites
def startup_refresh_catalog() -> bool:
    return refresh_catalog_with_logging(None, context="startup")
