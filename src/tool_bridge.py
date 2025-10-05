from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from pathlib import Path
import json


class ToolBridge:
    """Bridge to let cogs/tools pass compact results to the LLM in persona voice.

    This runs a single-shot prompt: persona+system plus one small [Tool] system block; no history.
    It uses the existing template engine, tokenizer, and LLM client held by the router.
    """

    def __init__(self, router, logger: Optional[logging.Logger] = None):
        self.router = router
        self.log = logger or logging.getLogger(__name__)

    async def run(
        self,
        channel_id: str,
        tool_name: str,
        intent: str,
        summary: str,
        details: Optional[str] = None,
        block_char_limit: int = 1024,
        is_nsfw: bool = False,
        temperature: Optional[float] = None,
        style_hint: Optional[str] = None,
    ) -> str | None:
        """Return persona-styled reply for a tool result.

        - channel_id: used for correlation and nsfw system path selection.
        - tool_name: short id (e.g., "ip").
        - intent: short verb-noun (e.g., "provide_external_ip").
        - summary: the key result to present.
        - details: optional short key=value string (already redacted).
        - block_char_limit: safety cap for the [Tool] block.
        - is_nsfw: uses nsfw system prompt variant when true.
        """
        if not getattr(self.router, "llm", None):
            return None
        # System message from template engine (nsfw-aware)
        system_text = self.router.tmpl.build_system_message_for(is_nsfw=is_nsfw)
        system_msg = {"role": "system", "content": system_text}
        # Build [Tool] system block and cap size
        lines = [
            f"[Tool] {tool_name}",
            f"intent: {intent}",
            "task: Present the following result to the user clearly with your personas flair.",
            f"result: {summary}",
            "constraints:",
            "- Do not ask questions.",
            "- Do not request clarification.",
            "- Use persona voice.",
            "- One short sentence unless the style hint asks otherwise.",
        ]
        if details:
            lines.append(f"details: {details}")
        if style_hint:
            lines.append(f"style_hint: {style_hint}")
        tool_block = "\n".join(lines)
        if len(tool_block) > block_char_limit:
            tool_block = tool_block[: block_char_limit - 3] + "..."
        tool_msg = {"role": "system", "content": tool_block}

        # No history, no template context; single user nudge keeps roles consistent
        user_msg = {"role": "user", "content": "Return only the final answer now. Do not ask any questions."}
        messages = [system_msg, tool_msg, user_msg]

        # Model selection & generation (reusing router model config)
        cfg = self.router.model_cfg or {}
        models = cfg.get("models")
        if isinstance(models, str):
            models_to_try = [m.strip() for m in models.split(",") if m.strip()]
        elif isinstance(models, list):
            models_to_try = [str(m) for m in models if str(m).strip()]
        else:
            models_to_try = []
        allow_auto = bool(cfg.get("allow_auto_fallback", False))
        stops = cfg.get("stop")
        max_tokens = cfg.get("max_tokens")
        temp = temperature if temperature is not None else self.router.model_cfg.get("temperature")

        correlation_id = f"{channel_id}-tool-{tool_name}-{int(datetime.now(timezone.utc).timestamp()*1000)}"
        reply = None
        model_used = None
        for idx, model_name in enumerate(models_to_try):
            if not model_name:
                continue
            try:
                self.log.info(f"[tool-llm-start] channel={channel_id} tool={tool_name} model={model_name} fallback_index={idx} correlation={correlation_id}")
                result = await self.router.llm.generate_chat(
                    messages,
                    max_tokens=max_tokens,
                    model=model_name,
                    temperature=temp,
                    top_p=self.router.model_cfg.get("top_p"),
                    stop=stops,
                    context_fields={"channel": channel_id, "user": "tool", "correlation": correlation_id},
                )
                reply = result.get("text") if isinstance(result, dict) else result
                model_used = model_name
                self.log.info(f"[tool-llm-finish] channel={channel_id} tool={tool_name} model={model_name} correlation={correlation_id}")
                break
            except Exception as e:
                self.log.error(f"tool-llm-error model={model_name} tool={tool_name} err={e}")

        if reply is None and allow_auto:
            try:
                result = await self.router.llm.generate_chat(
                    messages,
                    max_tokens=max_tokens,
                    model="openrouter/auto",
                    temperature=temp,
                    top_p=self.router.model_cfg.get("top_p"),
                    stop=stops,
                    context_fields={"channel": channel_id, "user": "tool", "correlation": correlation_id},
                )
                reply = result.get("text") if isinstance(result, dict) else result
                model_used = "openrouter/auto"
                self.log.info(f"[tool-llm-finish] channel={channel_id} tool={tool_name} model=openrouter/auto correlation={correlation_id}")
            except Exception as e2:
                self.log.error(f"tool-llm-auto-error tool={tool_name} err={e2}")

        # Optional prompt/response logging when enabled
        try:
            from .config_service import ConfigService
            cfg = ConfigService('config.yaml')
            if bool(cfg.log_prompts()):
                ts_dir = datetime.now().strftime('prompts-%Y%m%d-%H%M%S')
                out_dir = Path('logs') / ts_dir
                out_dir.mkdir(parents=True, exist_ok=True)
                p = {
                    'correlation': correlation_id,
                    'channel': channel_id,
                    'user': 'tool',
                    'model': model_used,
                    'tool': tool_name,
                    'messages': messages,
                }
                (out_dir / 'prompt.json').write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding='utf-8')
                lines = []
                for m in messages:
                    lines.append(f"[{m.get('role','')}] {m.get('content','')}")
                (out_dir / 'prompt.txt').write_text("\n\n".join(lines), encoding='utf-8')
                (out_dir / 'response.txt').write_text(str(reply or ''), encoding='utf-8')
        except Exception:
            pass

        return reply
