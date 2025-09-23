from __future__ import annotations

from typing import List, Dict


class TokenizerService:
    def __init__(self, chars_per_token: float = 4.0):
        # Heuristic: ~4 chars per token is a common rule-of-thumb for English
        self.chars_per_token = max(1e-6, float(chars_per_token))

    def estimate_tokens_text(self, text: str) -> int:
        if not text:
            return 0
        return max(1, int(len(text) / self.chars_per_token))

    def _estimate_content_tokens(self, content) -> int:
        # content may be str or a list of parts (OpenAI-style multimodal)
        if content is None:
            return 0
        if isinstance(content, str):
            return self.estimate_tokens_text(content)
        # list of parts: {type: "text"|"image_url"|..., ...}
        try:
            total = 0
            for part in content:
                ptype = str(part.get("type", "")).lower()
                if ptype == "text":
                    total += self.estimate_tokens_text(str(part.get("text", "")))
                elif ptype == "image_url":
                    # Assign a small fixed heuristic per image to represent prompt overhead
                    total += 64
                else:
                    # Unknown part type: negligible
                    total += 0
            return total
        except Exception:
            return self.estimate_tokens_text(str(content))

    def estimate_tokens_messages(self, messages: List[Dict]) -> int:
        total = 0
        for m in messages:
            total += self._estimate_content_tokens(m.get("content", ""))
        return total

    def truncate_text_tokens(self, text: str, max_tokens: int) -> str:
        if max_tokens <= 0:
            return ""
        approx_chars = int(max_tokens * self.chars_per_token)
        if len(text) <= approx_chars:
            return text
        # Try to cut on a boundary and add ellipsis
        cut = max(0, approx_chars - 3)
        return text[:cut] + "..."
