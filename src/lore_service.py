from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import re


@dataclass
class LoreEntry:
    uid: str
    keys: list[str] | None
    content: str
    comment: str | None
    source: str  # 'md' or 'json'
    constant: bool  # SillyTavern 'constant' flag (always on)


class LoreService:
    def __init__(self, paths: list[str], md_priority: str = "low"):
        self._entries: list[LoreEntry] = []
        self._md_priority = "high" if str(md_priority).lower() == "high" else "low"
        for p in paths:
            try:
                path = Path(p)
                if not path.exists() or not path.is_file():
                    continue
                suffix = path.suffix.lower()
                # Support Markdown files as always-on lore blocks
                if suffix in (".md", ".markdown"):
                    try:
                        text = path.read_text(encoding="utf-8")
                    except Exception:
                        continue
                    uid = path.stem
                    # Markdown lore is always-on (keys=None), optional comment from first heading if present
                    first_line = text.splitlines()[0].strip() if text else ""
                    comment = None
                    m = re.match(r"^\s*#+\s*(.+)$", first_line)
                    if m:
                        comment = m.group(1).strip()
                    self._entries.append(LoreEntry(uid=str(uid), keys=None, content=str(text), comment=comment, source="md", constant=True))
                    continue
                # JSON SillyTavern-like format
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    # Not JSON or invalid JSON; skip
                    continue
                entries = data.get("entries", {})
                for uid, raw in entries.items():
                    if not isinstance(raw, dict):
                        continue
                    content = raw.get("content")
                    if not content:
                        continue
                    keys = raw.get("key")
                    if keys is not None and not isinstance(keys, list):
                        # normalize unexpected shapes
                        keys = [str(keys)]
                    comment = raw.get("comment")
                    constant = bool(raw.get("constant", False))
                    self._entries.append(LoreEntry(uid=str(uid), keys=keys, content=str(content), comment=str(comment) if comment else None, source="json", constant=constant))
            except Exception:
                # ignore load errors per file to avoid crashing the bot
                continue

    def build_lore_block(self, corpus_text: str, max_tokens: int, tokenizer, logger=None) -> str | None:
        if not self._entries:
            return None
        text_lower = corpus_text.lower()
        # Partition entries: track Markdown vs JSON, always-on first (keys is None or empty), then matched
        always_md: list[LoreEntry] = []
        always_json: list[LoreEntry] = []
        matched_md: list[LoreEntry] = []
        matched_json: list[LoreEntry] = []
        for e in self._entries:
            if e.constant:
                (always_md if e.source == "md" else always_json).append(e)
                continue
            # For non-constant entries, include only if a key matches
            try:
                keys = e.keys or []
                if any(self._key_matches(text_lower, str(k)) for k in keys):
                    (matched_md if e.source == "md" else matched_json).append(e)
            except Exception:
                pass
        # Order based on md_priority
        if self._md_priority == "high":
            ordered = always_md + always_json + matched_md + matched_json
        else:
            ordered = always_json + always_md + matched_json + matched_md
        if not ordered:
            return None

        pieces: list[str] = []
        tokens_used = 0
        for e in ordered:
            # Each entry renders as an h2-like block with optional comment title
            header = f"## {e.comment}\n" if e.comment else ""
            block = f"{header}{e.content}\n\n"
            block_tokens = tokenizer.estimate_tokens_text(block)
            if tokens_used + block_tokens > max_tokens:
                # If nothing has been added yet, allow truncation to fit
                if tokens_used == 0:
                    truncated = tokenizer.truncate_text_tokens(block, max_tokens)
                    pieces.append(truncated)
                    tokens_used = max_tokens
                    if logger:
                        try:
                            logger.debug(f"lore-include uid={e.uid} title={e.comment or ''} tokens={max_tokens} cumulative={tokens_used} (truncated)")
                        except Exception:
                            pass
                break
            pieces.append(block)
            tokens_used += block_tokens
            if logger:
                try:
                    logger.debug(f"lore-include uid={e.uid} title={e.comment or ''} tokens={block_tokens} cumulative={tokens_used}")
                except Exception:
                    pass
        # If we exited because budget reached and there are more entries remaining, log the limit event
        if len(pieces) > 0 and (tokens_used >= max_tokens) and (len(ordered) > len(pieces)):
            if logger:
                try:
                    logger.debug(f"lore-limit-reached tokens_used={tokens_used} budget={max_tokens}")
                except Exception:
                    pass
        if not pieces:
            return None
        return "[Lore]\n" + "".join(pieces)

    @staticmethod
    def _key_matches(text_lower: str, key: str) -> bool:
        """Return True if key matches the text as a whole token/phrase.

        Rules:
        - For Latin/ASCII keys (letters/digits/space/punct), require word boundaries
          around the full key (e.g., 'Ai' doesn't match 'main'). Multi-word phrases
          allow flexible whitespace.
        - For CJK keys, fall back to substring match (common segmentation rules differ).
        """
        try:
            k = (key or "").strip()
            if not k:
                return False
            k_lower = k.lower()
            # CJK ranges: Hiragana, Katakana, CJK Unified, Halfwidth Katakana
            if re.search(r"[\u3040-\u30ff\u3400-\u9fff\uff66-\uff9d]", k_lower):
                return k_lower in text_lower
            # Build a word-boundary regex for Latin keys with exact literal spacing
            escaped = re.escape(k_lower)
            pattern = rf"\b{escaped}\b"
            return re.search(pattern, text_lower, flags=re.IGNORECASE) is not None
        except Exception:
            # On regex error, fall back to conservative equality check
            return False
