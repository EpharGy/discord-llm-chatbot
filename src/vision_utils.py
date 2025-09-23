from __future__ import annotations

import re
from typing import List

try:
    import discord
except Exception:  # pragma: no cover - type hints only
    discord = None  # type: ignore


IMG_EXT_RE = re.compile(r"\.(png|jpe?g|gif|webp)$", re.IGNORECASE)


def _maybe_add(urls: List[str], seen: set, url: str | None) -> None:
    if not url:
        return
    if url in seen:
        return
    seen.add(url)
    urls.append(url)


def extract_image_urls(message) -> List[str]:
    """Return a de-duplicated list of image URLs from a Discord message.

    Sources:
    - Attachments with content_type starting with image/
    - Embeds with image or thumbnail URLs
    - Plain-text URLs with common image extensions
    """
    urls: List[str] = []
    seen = set()

    # Attachments (uploads)
    for att in getattr(message, "attachments", []) or []:
        try:
            ctype = getattr(att, "content_type", "") or ""
            name = getattr(att, "filename", "") or ""
            url = getattr(att, "url", None)
            proxy = getattr(att, "proxy_url", None)
            height = getattr(att, "height", None)
            width = getattr(att, "width", None)
            looks_like_image = (
                ctype.startswith("image/")
                or IMG_EXT_RE.search(name)
                or IMG_EXT_RE.search(str(url or ""))
                or (height is not None or width is not None)
            )
            if looks_like_image:
                for u in (url, proxy):
                    _maybe_add(urls, seen, u)
                    if u:
                        break
        except Exception:
            continue

    # Embeds (link previews etc.)
    for emb in getattr(message, "embeds", []) or []:
        try:
            img = getattr(emb, "image", None)
            thumb = getattr(emb, "thumbnail", None)
            for obj in (img, thumb):
                if obj is None:
                    continue
                u = getattr(obj, "url", None)
                _maybe_add(urls, seen, u)
            # Fallback: embed.url itself may be a direct image
            eu = getattr(emb, "url", None)
            etype = str(getattr(emb, "type", "") or "").lower()
            if eu and (etype == "image" or IMG_EXT_RE.search(str(eu))):
                _maybe_add(urls, seen, eu)
        except Exception:
            continue

    # Text URLs (basic scan)
    try:
        content = getattr(message, "content", "") or ""
        for m in re.finditer(r"https?://\S+", content):
            u = m.group(0)
            if IMG_EXT_RE.search(u):
                _maybe_add(urls, seen, u)
    except Exception:
        pass

    return urls
