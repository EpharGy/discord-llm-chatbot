from __future__ import annotations

from typing import Optional, Sequence
from .base import LLMClient
from ..logger_factory import get_logger


class MultiBackendClient(LLMClient):
    """Tries multiple LLM backends in order until one succeeds.

    Provide a list of callables that return an LLMClient (or the client instances themselves).
    The wrapper will call generate_chat/text on each until one returns a response.
    """

    def __init__(self, providers: list[LLMClient]):
        self.providers = providers

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
        last_exc: Exception | None = None
        for p in self.providers:
            try:
                return await p.generate_chat(
                    messages,
                    max_tokens=max_tokens,
                    model=model,
                    temperature=temperature,
                    top_p=top_p,
                    frequency_penalty=frequency_penalty,
                    presence_penalty=presence_penalty,
                    stop=stop,
                    context_fields=context_fields,
                )
            except Exception as e:
                last_exc = e
                continue
        if last_exc:
            raise last_exc
        raise RuntimeError("No LLM providers configured")

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
        last_exc: Exception | None = None
        for p in self.providers:
            try:
                return await p.generate_text(
                    prompt,
                    stop=stop,
                    max_tokens=max_tokens,
                    model=model,
                    temperature=temperature,
                    top_p=top_p,
                    context_fields=context_fields,
                )
            except Exception as e:
                last_exc = e
                continue
        if last_exc:
            raise last_exc
        raise RuntimeError("No LLM providers configured")


class ContextualMultiBackendClient(LLMClient):
    """Chooses provider list based on context flags (vision/nsfw/normal).

    Expects context_fields to optionally include:
    - has_images: bool -> use vision providers when True and available
    - nsfw: bool -> use nsfw providers when True and no vision context
    Fallback order: vision > nsfw > normal.
    """

    def __init__(
        self,
        *,
        normal: list[LLMClient],
        nsfw: list[LLMClient] | None = None,
        vision: list[LLMClient] | None = None,
        web: list[LLMClient] | None = None,
    ):
        self.normal = normal or []
        self.nsfw = nsfw or []
        self.vision = vision or []
        self.web = web or []
        self.log = get_logger("LLMSelect")

    def _select(self, context_fields: Optional[dict]) -> list[LLMClient]:
        cf = context_fields or {}
        # Precedence: images -> web -> nsfw -> normal
        if cf.get("has_images") and self.vision:
            return self.vision
        if cf.get("web") and self.web:
            return self.web
        if cf.get("nsfw") and self.nsfw:
            return self.nsfw
        # Default to normal when no context flags apply
        return self.normal

    # Expose provider list for outer control (e.g., router-level provider-first fallback)
    def providers_for_context(self, context_fields: Optional[dict]) -> list[LLMClient]:
        return list(self._select(context_fields))

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
        cf = context_fields or {}
        providers = self._select(cf)
        if not providers:
            raise RuntimeError("No LLM providers configured for context")
        idx = int(cf.get("provider_index", 0)) if isinstance(cf.get("provider_index", 0), (int, float)) else 0
        if idx < 0 or idx >= len(providers):
            idx = 0
        p = providers[idx]
        chan = cf.get('channel')
        user = cf.get('user')
        corr = cf.get('correlation')
        nsfw = bool(cf.get('nsfw'))
        has_images = bool(cf.get('has_images'))
        # Unified start log with provider and model label; normalize model for openai-compat
        model_disp = 'openai-compat' if p.__class__.__name__ == 'OpenAICompatClient' else model
        self.log.info(
            f"[llm-start] channel={chan!r} user={user!r} model={model_disp!r} provider={p.__class__.__name__} nsfw={nsfw} has_images={has_images} correlation={corr!r}"
        )
        return await p.generate_chat(
            messages,
            max_tokens=max_tokens,
            model=model,
            temperature=temperature,
            top_p=top_p,
            frequency_penalty=frequency_penalty,
            presence_penalty=presence_penalty,
            stop=stop,
            context_fields=context_fields,
        )

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
        cf = context_fields or {}
        providers = self._select(cf)
        if not providers:
            raise RuntimeError("No LLM providers configured for context")
        idx = int(cf.get("provider_index", 0)) if isinstance(cf.get("provider_index", 0), (int, float)) else 0
        if idx < 0 or idx >= len(providers):
            idx = 0
        p = providers[idx]
        chan = cf.get('channel')
        user = cf.get('user')
        corr = cf.get('correlation')
        nsfw = bool(cf.get('nsfw'))
        has_images = bool(cf.get('has_images'))
        model_disp = 'openai-compat' if p.__class__.__name__ == 'OpenAICompatClient' else model
        self.log.info(
            f"[llm-start] channel={chan!r} user={user!r} model={model_disp!r} provider={p.__class__.__name__} nsfw={nsfw} has_images={has_images} correlation={corr!r}"
        )
        return await p.generate_text(
            prompt,
            stop=stop,
            max_tokens=max_tokens,
            model=model,
            temperature=temperature,
            top_p=top_p,
            context_fields=context_fields,
        )
