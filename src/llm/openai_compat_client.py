from __future__ import annotations

import asyncio
from typing import Optional, Sequence
import httpx
from .base import LLMClient
from ..logger_factory import get_logger
from ..utils.logfmt import fmt


class OpenAICompatClient(LLMClient):
    """Minimal OpenAI-compatible chat client for local backends (e.g., llama.cpp, LM Studio, textgen-webui in OpenAI mode).

    Assumes base_url like http://127.0.0.1:5001/v1/chat/completions (no auth header).
    """

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:5001/v1/chat/completions",
        concurrency: int = 2,
        timeout: float = 60.0,
        retry_attempts: int = 1,
    ):
        self.log = get_logger("OpenAICompat")
        # Normalize: accept /v1, /v1/chat/completions, or /v1/completions
        u = base_url.rstrip("/")
        if u.endswith("/v1"):
            self.chat_url = u + "/chat/completions"
            self.comp_url = u + "/completions"
        elif u.endswith("/chat/completions"):
            self.chat_url = u
            self.comp_url = u.rsplit("/chat/completions", 1)[0] + "/completions"
        elif u.endswith("/completions"):
            self.comp_url = u
            self.chat_url = u.rsplit("/completions", 1)[0] + "/chat/completions"
        else:
            # Best-effort: treat as base and append endpoints
            self.chat_url = u + "/v1/chat/completions"
            self.comp_url = u + "/v1/completions"
        self.timeout = timeout
        self.retry_attempts = max(0, int(retry_attempts))
        self._sem = asyncio.Semaphore(concurrency)
        self._client = httpx.AsyncClient(timeout=timeout)

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
        messages = [{"role": "user", "content": prompt}]
        return await self.generate_chat(
            messages,
            max_tokens=max_tokens,
            model=model,
            temperature=temperature,
            top_p=top_p,
            stop=stop,
            context_fields=context_fields,
        )

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
        # Detect whether this payload includes images (OpenAI-style content parts)
        def _has_images(msgs: list[dict]) -> bool:
            try:
                for m in msgs:
                    c = m.get("content")
                    if isinstance(c, list):
                        for it in c:
                            if isinstance(it, dict) and it.get("type") in ("image_url", "image"):
                                return True
                return False
            except Exception:
                return False

        includes_images = _has_images(messages)
        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "top_p": top_p,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if stop:
            payload["stop"] = list(stop)
        if frequency_penalty is not None:
            payload["frequency_penalty"] = frequency_penalty
        if presence_penalty is not None:
            payload["presence_penalty"] = presence_penalty

        attempts = 0
        max_attempts = 1 + self.retry_attempts
        last_exc: Optional[Exception] = None
        data = None
        async with self._sem:
            while attempts < max_attempts:
                attempts += 1
                try:
                    # First try chat/completions
                    r = await self._client.post(self.chat_url, json=payload)
                    r.raise_for_status()
                    data = r.json()
                    break
                except httpx.HTTPStatusError as e:
                    # If chat endpoint is missing, try /v1/completions by flattening messages
                    status = e.response.status_code if e.response is not None else 0
                    if status in (404, 405):
                        # If images are present, do NOT fallback to /completions (would drop images)
                        if includes_images:
                            last_exc = e
                            raise RuntimeError("openai-compat-chat-endpoint-missing-for-images") from e
                        # Build a simple prompt from messages
                        try:
                            parts: list[str] = []
                            for m in messages:
                                role = m.get("role", "user")
                                content = m.get("content")
                                if isinstance(content, list):
                                    # Multimodal: keep text parts only
                                    text_chunks = []
                                    for it in content:
                                        if isinstance(it, dict) and it.get("type") == "text":
                                            text_chunks.append(str(it.get("text", "")))
                                    content = "\n".join([c for c in text_chunks if c])
                                if content is None:
                                    continue
                                parts.append(f"{role}: {content}")
                            prompt = "\n\n".join(parts)
                        except Exception:
                            prompt = "\n\n".join(str(m.get("content", "")) for m in messages)
                        comp_payload: dict = {
                            "model": model,
                            "prompt": prompt,
                            "temperature": temperature,
                            "top_p": top_p,
                        }
                        if max_tokens is not None:
                            comp_payload["max_tokens"] = max_tokens
                        if stop:
                            comp_payload["stop"] = list(stop)
                        rc = await self._client.post(self.comp_url, json=comp_payload)
                        rc.raise_for_status()
                        data = rc.json()
                        break
                    last_exc = e
                    if attempts < max_attempts:
                        await asyncio.sleep(min(1.0 * attempts, 3.0))
                    else:
                        raise
                except Exception as e:
                    last_exc = e
                    if attempts < max_attempts:
                        await asyncio.sleep(min(1.0 * attempts, 3.0))
                    else:
                        raise

        try:
            assert data is not None
            # Support both chat and completions response shapes
            ch = data.get("choices")[0]
            if "message" in ch and ch["message"] and "content" in ch["message"]:
                mc = ch["message"]["content"]
                if isinstance(mc, list):
                    # Some servers may return an array of content parts
                    parts: list[str] = []
                    for it in mc:
                        if isinstance(it, dict):
                            if it.get("type") == "text" and it.get("text"):
                                parts.append(str(it.get("text")))
                    text = "\n".join(parts).strip()
                else:
                    text = (mc or "").strip()
            else:
                text = (ch.get("text", "") or "").strip()
        except Exception as e:
            raise RuntimeError(f"OpenAI-compatible response parse error: {data}") from e
        usage = data.get("usage") or {}
        input_tokens = usage.get("prompt_tokens") or usage.get("input_tokens")
        output_tokens = usage.get("completion_tokens") or usage.get("output_tokens")
        total_tokens = usage.get("total_tokens")
        cf = context_fields or {}
        # Keep internal finish log at debug to avoid duplicate with router-level [llm-finish]
        self.log.debug(f"[llm-provider-finish] {fmt('provider','openai')} {fmt('channel', cf.get('channel'))} {fmt('user', cf.get('user'))} {fmt('correlation', cf.get('correlation'))}")
        return {
            "text": text,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
            },
            "provider": "openai",
            "model": model,
        }

    async def aclose(self):
        await self._client.aclose()
