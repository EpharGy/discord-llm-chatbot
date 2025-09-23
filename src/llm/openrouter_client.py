from __future__ import annotations

import os
import asyncio
import httpx
import random
from typing import Optional, Sequence
from .base import LLMClient
from ..logger_factory import get_logger
from ..utils.logfmt import fmt


class OpenRouterClient(LLMClient):
    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: str = "https://openrouter.ai/api/v1/chat/completions",
        concurrency: int = 2,
        timeout: float = 60.0,
        retry_attempts: int = 2,  # number of retries on transient errors (total attempts = 1 + retries)
        http_referer: Optional[str] = None,
        x_title: Optional[str] = "Discord LLM Bot",
    ):
        self.log = get_logger("OpenRouter")
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY")
        if not self.api_key:
            raise RuntimeError("Missing OPENROUTER_API_KEY in environment or constructor")
        self.base_url = base_url
        self.timeout = timeout
        self.retry_attempts = max(0, int(retry_attempts))
        self.http_referer = http_referer
        self.x_title = x_title
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

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # Optional ranking headers for OpenRouter
        if self.http_referer:
            headers["HTTP-Referer"] = self.http_referer
        if self.x_title:
            headers["X-Title"] = self.x_title

        # Simple retry with backoff + jitter for 5xx, 429, timeouts, and transient network issues
        attempts = 0
        max_attempts = 1 + self.retry_attempts
        last_exc: Optional[Exception] = None
        async with self._sem:
            while attempts < max_attempts:
                attempts += 1
                try:
                    r = await self._client.post(self.base_url, json=payload, headers=headers)
                    r.raise_for_status()
                    data = r.json()
                    break
                except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError) as e:
                    last_exc = e
                    status = "timeout"
                except httpx.HTTPStatusError as e:  # type: ignore[name-defined]
                    last_exc = e
                    code = e.response.status_code if e.response is not None else 0
                    if code in (429, 500, 502, 503, 504):
                        status = code
                    else:
                        body = e.response.text if e.response is not None else "<no body>"
                        raise RuntimeError(
                            f"OpenRouter HTTP error {code}: {body[:500]}"
                        ) from e

                # Backoff and retry if attempts remain
                if attempts < max_attempts:
                    base = 0.25  # seconds
                    backoff = base * (2 ** (attempts - 1))
                    jitter = backoff * (0.5 + random.random() * 0.5)  # 50%..100% of backoff
                    delay = backoff + jitter
                    cf = context_fields or {}
                    self.log.info(
                        f"llm-retry {fmt('model', model)} {fmt('attempt', attempts)} {fmt('backoff_ms', int(delay*1000))} "
                        f"{fmt('status', status)} {fmt('correlation', cf.get('correlation'))} "
                        f"{fmt('channel', cf.get('channel'))} {fmt('user', cf.get('user'))}"
                    )
                    await asyncio.sleep(delay)
            else:
                # Exceeded attempts
                raise RuntimeError(f"OpenRouter retries exhausted: {last_exc}") from last_exc
            try:
                text = data["choices"][0]["message"]["content"].strip()
            except Exception as e:
                raise RuntimeError(f"OpenRouter response parse error: {data}") from e
            usage = data.get("usage") or {}
            # Normalize usage fields if present
            input_tokens = usage.get("prompt_tokens") or usage.get("input_tokens")
            output_tokens = usage.get("completion_tokens") or usage.get("output_tokens")
            total_tokens = usage.get("total_tokens")
            return {
                "text": text,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                },
            }

    async def aclose(self):
        await self._client.aclose()
