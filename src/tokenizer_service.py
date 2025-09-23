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

    def estimate_tokens_messages(self, messages: List[Dict]) -> int:
        total = 0
        for m in messages:
            total += self.estimate_tokens_text(str(m.get("content", "")))
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
