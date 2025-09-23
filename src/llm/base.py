from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, Sequence


class LLMClient(ABC):
    @abstractmethod
    async def generate_chat(
        self,
        messages: list[dict],
        *,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        frequency_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        stop: Optional[Sequence[str]] = None,
        context_fields: Optional[dict] = None,
    ) -> dict:
        ...
    @abstractmethod
    async def generate_text(
        self,
        prompt: str,
        *,
        stop: Optional[Sequence[str]] = None,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        context_fields: Optional[dict] = None,
    ) -> dict:
        ...
