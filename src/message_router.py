from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta as _timedelta
from pathlib import Path
from datetime import datetime as dt
import io
from .task_queue import PendingMention
import discord
from .utils.correlation import make_correlation_id
from .utils.logfmt import fmt
import re


@dataclass
class ChatMessage:
    role: str
    content: str
    author: str | None = None
    timestamp_iso: str | None = None


class MessageRouter:
    def __init__(self, template_engine, tokenizer, memory, policy, logger, mentions_queue=None, batcher=None, llm=None, model_cfg=None, lore=None, lore_config: dict | None = None):
        self.tmpl = template_engine
        self.tok = tokenizer
        self.memory = memory
        self.policy = policy
        self.log = logger
        self.queue = mentions_queue
        self.batcher = batcher
        self.llm = llm
        self.model_cfg = model_cfg or {}
        self.lore = lore
        self.lore_cfg = lore_config or {"enabled": False, "max_fraction": 0.33}

    def _split_for_discord(self, text: str) -> list[str]:
        # Split into at most config.max_response_messages parts, respecting discord.message_char_limit
        try:
            from .config_service import ConfigService
            cfg = ConfigService("config.yaml")
            limit = max(1, int(cfg.discord_message_char_limit()))
            max_parts = max(1, int(cfg.max_response_messages()))
        except Exception:
            limit = 2000
            max_parts = 2
        if not text or len(text) <= limit:
            return [text]
        parts: list[str] = []
        remaining = text
        # Reserve room for ellipsis markers when splitting
        cont_marker = " ....."
        lead_marker = "..... "
        # First part: end with cont_marker if more remains
        for i in range(max_parts):
            if len(remaining) <= limit:
                parts.append(remaining)
                break
            # pick slice length accounting for continuation markers
            slice_limit = limit - (len(cont_marker) if i == 0 else 0)
            if slice_limit <= 0:
                slice_limit = limit
            chunk = remaining[:slice_limit]
            # try to cut on last whitespace
            cut = chunk.rfind(" ")
            if cut < int(slice_limit * 0.6):
                cut = slice_limit  # no good whitespace; hard cut near limit
            head = remaining[:cut].rstrip()
            tail = remaining[cut:].lstrip()
            if i == 0:
                parts.append((head + cont_marker)[:limit])
            else:
                # Subsequent first chunk should include leading marker, within limit
                chunk2 = (lead_marker + head)
                if len(chunk2) > limit:
                    chunk2 = chunk2[:limit]
                parts.append(chunk2)
            remaining = tail
            if i == max_parts - 1 and remaining:
                # append the rest to last part truncated to limit with leading marker
                final = (lead_marker + remaining)
                parts[-1] = final[:limit]
                remaining = ""
                break
        return parts

    async def handle_message(self, message):
        # Resolve channel/guild display names (prefer names, fallback to IDs)
        try:
            channel_name = getattr(message.channel, "name", None) or str(message.channel.id)
        except Exception:
            channel_name = str(getattr(message.channel, "id", "unknown"))
        try:
            guild_name = getattr(message.guild, "name", None) or ("DM" if getattr(message.channel, "type", None) and str(message.channel.type).lower().endswith("dm") else "unknown")
        except Exception:
            guild_name = "unknown"
        event = {
            "channel_id": str(message.channel.id),
            "channel_name": channel_name,
            "author_id": str(message.author.id),
            "message_id": str(message.id),
            "content": message.content or "",
            "mentions": [m.id for m in message.mentions],
            "is_bot": bool(getattr(message.author, "bot", False)),
            "created_at": message.created_at.replace(tzinfo=timezone.utc),
            "author_name": message.author.display_name,
            "guild_name": guild_name,
        }

        # Determine bot user id
        bot_id = None
        try:
            if message.client and message.client.user:
                bot_id = message.client.user.id
        except Exception:
            bot_id = getattr(getattr(message.guild, "me", None), "id", None)

        # Mentions
        event["is_mentioned"] = bool(bot_id and any(mid == bot_id for mid in event["mentions"]))

        # Reply metadata
        event["is_reply"] = bool(message.reference and getattr(message.reference, "message_id", None))
        event["is_reply_to_bot"] = False
        if event["is_reply"]:
            parent = None
            ref = message.reference
            # Prefer cached resolved message to avoid network calls
            parent = getattr(ref, "resolved", None) or getattr(ref, "cached_message", None)
            if isinstance(parent, discord.Message):
                try:
                    event["reply_to_author_id"] = str(parent.author.id)
                    event["reply_to_is_bot"] = bool(getattr(parent.author, "bot", False))
                    event["reply_to_message_content"] = parent.content or ""
                    event["reply_to_author_name"] = getattr(parent.author, "display_name", None) or ("bot" if getattr(parent.author, "bot", False) else "user")
                    if bot_id and parent.author.id == bot_id:
                        event["is_reply_to_bot"] = True
                except Exception:
                    pass

        # Determine direct message trigger by name/mention
        content_lower = (event.get("content") or "").lower()
        name_matched = any(alias in content_lower for alias in self.policy.aliases) if getattr(self.policy, "respond_to_name", False) else False
        # Treat replies to the bot as direct triggers as well
        is_direct = bool(event.get("is_mentioned") or name_matched or event.get("is_reply_to_bot"))
        allowed_channel = event["channel_id"] in getattr(self.policy, "allowed_general_channels", set())

        # Record only if allowed channel for general chat OR it's a direct trigger
        if is_direct or allowed_channel:
            self.memory.record(event)

        # Correlation id ties decision → LLM call → send
        correlation_id = make_correlation_id(event['channel_id'], getattr(message, 'id', 'msg'))

        event["correlation"] = correlation_id
        # Guard: do not respond twice to the same message id
        try:
            if self.memory.has_responded_to(event["channel_id"], event.get("message_id")):
                self.log.debug(f"skip-duplicate {fmt('channel', event.get('channel_name', event['channel_id']))} {fmt('msg', getattr(message, 'id', ''))}")
                return
        except Exception:
            pass
        decision = self.policy.should_reply(event, self.memory)
        # Conversation mode: if a window is active in this channel, auto-allow up to a message budget
        _conv_cfg = getattr(self.policy, "conversation_mode", None)
        if decision.get("allow") is False and getattr(self.memory, "conversation_mode_active", None):
            if self.memory.conversation_mode_active(event["channel_id"]):
                cm = getattr(self.policy, "conversation_mode", {})
                allow_non_replies = bool(cm.get("include_non_replies", False))
                mention_required = bool(getattr(self.policy, "mention_required", False))
                content_lower = (event.get("content") or "").lower()
                name_matched = any(alias in content_lower for alias in getattr(self.policy, "aliases", [])) if getattr(self.policy, "respond_to_name", False) else False
                is_direct = bool(event.get("is_mentioned") or name_matched)

                # Mentions/names are always allowed during the window and do not consume budget
                if is_direct:
                    decision = {"allow": True, "reason": "conversation-mode", "style": "reply" if event.get("is_reply") else "normal"}
                else:
                    # If mention is required, do not auto-allow non-mentions during the window
                    if mention_required:
                        pass  # keep decision as not allowed
                    else:
                        # Respect channel allowlist: in disallowed channels, only auto-allow replies; in allowed channels, include non-replies if configured
                        allow_this = False
                        if allowed_channel:
                            allow_this = bool(event.get("is_reply") or allow_non_replies)
                        else:
                            allow_this = bool(event.get("is_reply"))
                        if allow_this and self.memory.consume_conversation_message(event["channel_id"]):
                            style = "reply" if event.get("is_reply") else "normal"
                            decision = {"allow": True, "reason": "conversation-mode", "style": style}
        # Log final decision with level based on allow
        allow_flag = bool(decision.get('allow'))
        line = (
            f"decision "
            f"{fmt('channel', event.get('channel_name', event['channel_id']))} "
            f"{fmt('user', event.get('author_name'))} "
            f"{fmt('allow', allow_flag)} "
            f"{fmt('reason', decision.get('reason'))} "
            f"{fmt('style', decision.get('style', 'normal'))} "
            f"{fmt('msg', getattr(message, 'id', ''))} "
            f"{fmt('correlation', correlation_id)}"
        )
        if allow_flag:
            self.log.info(line)
        else:
            self.log.debug(line)
        if not decision.get("allow"):
            # Ephemeral anti-spam: send a temporary notice if applicable
            if decision.get("ephemeral") and decision.get("reason") == "anti-spam":
                # If this was a mention (or name alias), enqueue it so it can be answered later
                content_lower = (event.get("content") or "").lower()
                name_matched = any(alias in content_lower for alias in self.policy.aliases)
                if self.queue and (event.get("is_mentioned") or name_matched):
                    enq_ok = self.queue.enqueue(PendingMention(channel_id=event["channel_id"], message_id=message.id, style="reply"))
                    if enq_ok:
                        notice = "You’re in a queue—will reply shortly when the channel cools down."
                    else:
                        notice = "Queue is busy—please try again in a moment."
                else:
                    notice = "I’m slowing down to avoid spam. Be right back soon."
                try:
                    warn = await message.channel.send(notice)
                    # Try to delete after a few seconds
                    # Note: Discord has rate limits; ignoring failures here
                    await warn.delete(delay=getattr(self.policy, "warning_ttl_seconds", 5))
                except Exception:
                    pass
            else:
                self.log.debug(f"Not replying: {decision.get('reason')}")
            return

        # If conversation mode is active and batcher is present, skip immediate reply; batch loop will handle it
        try:
            # Mentions must bypass batching (reply immediately). Only skip immediately if this message will be batched
            if self.batcher and self.memory.conversation_mode_active(event["channel_id"]) and not event.get("is_mentioned", False):
                if allowed_channel or event.get("is_reply"):
                    # Ensure recorded (if not previously) and enqueue for batch processing
                    if not (is_direct or allowed_channel):
                        self.memory.record(event)
                    try:
                        self.batcher.add(event["channel_id"], event)
                        self.log.debug(f"batch-enqueue {fmt('channel', event.get('channel_name', event['channel_id']))} {fmt('msg', event.get('message_id',''))}")
                    except Exception:
                        pass
                    return
        except Exception:
            pass

        # Build prompt messages (simple baseline)
        # Build conversation window based on context hint (e.g., time-bound) or default
        context_hint = decision.get("context_hint")
        if context_hint and context_hint.get("time_bound_minutes"):
            from datetime import timedelta
            cutoff = event["created_at"] - timedelta(minutes=int(context_hint["time_bound_minutes"]))
            recent = [
                it for it in self.memory.get_recent(event["channel_id"], limit=self.policy.window_size())
                if it.get("created_at") and it["created_at"] >= cutoff
            ]
            # Cap by optional max_messages
            max_msgs = int(context_hint.get("max_messages", self.policy.window_size()))
            recent = recent[-max_msgs:]
        else:
            recent = self.memory.get_recent(event["channel_id"], limit=self.policy.window_size())
        # Note: Chat messages are constructed directly below using system/history/user blocks.

        # Prepare chat messages for LLM
        system_msg = {"role": "system", "content": self.tmpl.build_system_message()}
        # If using template, keep only a small tail of raw history without author brackets for continuity
        use_tmpl = False
        keep_tail = 2
        try:
            from .config_service import ConfigService
            cfg = ConfigService("config.yaml")
            use_tmpl = bool(cfg.use_template())
            keep_tail = int(cfg.keep_history_tail())
        except Exception:
            pass

        history = []
        structured_msgs = []
        for it in recent:
            role = "assistant" if it.get("is_bot") else "user"
            author = it.get("author_name") or ("bot" if it.get("is_bot") else "user")
            content = it.get("content", "")
            timestamp_iso = it.get("created_at").isoformat() if it.get("created_at") else None
            structured_msgs.append({"role": role, "author": author, "content": content, "timestamp_iso": timestamp_iso})
        # Re-cluster to prioritize the most recent conversation context
        try:
            from .config_service import ConfigService
            cfg = ConfigService("config.yaml")
            recency_min = int(cfg.recency_minutes())
            cluster_max = max(1, int(cfg.cluster_max_messages()))
            thread_max = max(0, int(cfg.thread_affinity_max()))
            now_ts = event.get("created_at") or datetime.now(timezone.utc)
            cutoff = now_ts - _timedelta(minutes=recency_min)
            def _parse_iso(ts: str | None):
                if not ts:
                    return None
                try:
                    t = dt.fromisoformat(ts)
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=timezone.utc)
                    return t
                except Exception:
                    return None
            recent_structs = []
            for m in structured_msgs:
                t = _parse_iso(m.get("timestamp_iso"))
                if (t is None) or (t >= cutoff):
                    recent_structs.append((t, m))
            if recent_structs:
                recent_structs.sort(key=lambda x: (x[0] or now_ts))
                clustered = [m for _, m in recent_structs][-cluster_max:]
                structured_msgs = clustered
            else:
                structured_msgs = structured_msgs[-cluster_max:]
            # Thread affinity: prefer recent turns between the current author and the bot
            try:
                current_author = event.get("author_name") or "user"
                tail_candidates = []
                for m in reversed([m for _, m in recent_structs] if recent_structs else structured_msgs):
                    if m.get("role") == "assistant" or (m.get("role") == "user" and (m.get("author") == current_author)):
                        tail_candidates.append(m)
                    if len(tail_candidates) >= thread_max:
                        break
                # Merge affinity tail at the end, preserving order and uniqueness
                tail_candidates = list(reversed(tail_candidates))
                seen = set()
                merged = []
                for m in structured_msgs + tail_candidates:
                    key = (m.get("role"), m.get("author"), m.get("content"))
                    if key in seen:
                        continue
                    seen.add(key)
                    merged.append(m)
                structured_msgs = merged[-cluster_max:]
            except Exception:
                pass
            # Ensure parent assistant message in the cluster if replying to the bot
            if event.get("is_reply_to_bot") and event.get("reply_to_message_content"):
                parent_content = event["reply_to_message_content"]
                if not any((m.get("role") == "assistant" and m.get("content") == parent_content) for m in structured_msgs):
                    structured_msgs.append({"role": "assistant", "author": "bot", "content": parent_content, "timestamp_iso": None})
        except Exception:
            pass
        if use_tmpl:
            tail = structured_msgs[-keep_tail:] if keep_tail > 0 else []
            for m in tail:
                history.append({"role": m["role"], "content": m["content"]})
        else:
            # Legacy: prefix only user messages
            for it in recent:
                role = "assistant" if it.get("is_bot") else "user"
                if it.get("is_bot"):
                    content = it.get("content", "")
                else:
                    author = it.get("author_name") or "user"
                    content = f"[{author}] {it.get('content', '')}"
                history.append({"role": role, "content": content})
        # If replying to a bot message, ensure the parent bot message is present near the end of history
        if event.get("is_reply_to_bot") and event.get("reply_to_message_content"):
            parent_content = event['reply_to_message_content']
            if all(h.get("content") != parent_content for h in history):
                history.append({"role": "assistant", "content": parent_content})
        # Prefix current message with author to give LLM context about the source (e.g., SRB)
        current_author = event.get("author_name") or ("bot" if event.get("is_bot") else "user")
        user_msg = {"role": "user", "content": f"[{current_author}] {event['content']}"}

    # Heuristic token budgeting: keep prompt within (max_context - response_tokens_max)
        try:
            from .config_service import ConfigService
            # The template engine and router share the same config instance at startup; here we reconstruct lightweightly
            cfg = ConfigService("config.yaml")
            max_ctx = int(cfg.max_context_tokens())
            reserve = int(cfg.response_tokens_max())
        except Exception:
            max_ctx = 8192
            reserve = 512
        prompt_budget = max(1, max_ctx - reserve)

        # Start message list with the primary system message
        system_ctx = None
        messages_for_est = [system_msg]

        # Lore injection: build corpus from the entire visible context (structured + user), and insert lore right after system
        builder = getattr(self.lore, "build_lore_block", None) if getattr(self, "lore", None) is not None else None
        if builder and self.lore_cfg.get("enabled"):
            lore_fraction = float(self.lore_cfg.get("max_fraction", 0.33))
            lore_budget = max(1, int(prompt_budget * lore_fraction))
            corpus_parts = [f"{m.get('author')}: {m.get('content')}" for m in structured_msgs]
            corpus_parts.append(event['content'])
            corpus = "\n".join(corpus_parts)
            lore_text = builder(corpus_text=corpus, max_tokens=lore_budget, tokenizer=self.tok, logger=self.log)
            if lore_text:
                # Nudge model: this is optional background to use if relevant
                messages_for_est.append({"role": "system", "content": "You may use the following background context if it is relevant to the user’s request.\n" + lore_text})

        # Subtle current local time hint (no location/zone), to help with time-related questions
        try:
            messages_for_est.append({"role": "system", "content": f"Current Date/Time: {datetime.now().strftime('%Y-%m-%d %H:%M')}"})
        except Exception:
            pass

        # If using template, render a context block and attach as an extra system message (after lore)
        if use_tmpl:
            context_block = self.tmpl.render(conversation_window=structured_msgs, user_input=event['content'], summary=None)
            system_ctx = {"role": "system", "content": context_block}
            messages_for_est.append(system_ctx)

        # Now append history tail and user
        messages_for_est.extend([*history, user_msg])
        tokens_before = self.tok.estimate_tokens_messages(messages_for_est)
        # If over budget, trim history from oldest while keeping parent-bot message if present
        if tokens_before > prompt_budget and history:
            # Identify if parent assistant message was injected at the end
            parent_assistant = None
            if event.get("is_reply_to_bot") and event.get("reply_to_message_content"):
                parent_assistant = {"role": "assistant", "content": event.get("reply_to_message_content")}
            trimmed = list(history)
            while trimmed and self.tok.estimate_tokens_messages([system_msg, *trimmed, user_msg]) > prompt_budget:
                # Avoid removing the parent assistant message if it exists and matches the last element
                if parent_assistant and trimmed and trimmed[0] == parent_assistant:
                    # Skip removing the parent; remove next oldest instead
                    if len(trimmed) > 1:
                        trimmed.pop(1)
                    else:
                        break
                else:
                    trimmed.pop(0)
            history = trimmed

        # If still over budget, truncate the user message content as last resort
        if self.tok.estimate_tokens_messages(messages_for_est) > prompt_budget:
            user_msg["content"] = self.tok.truncate_text_tokens(user_msg["content"], max_tokens=max(8, prompt_budget - self.tok.estimate_tokens_messages([system_msg, *history])))

        tokens_after = self.tok.estimate_tokens_messages(messages_for_est)
        try:
            self.log.debug(
                f"tokenizer-summary {fmt('channel', event.get('channel_name', event['channel_id']))} "
                f"{fmt('user', event.get('author_name'))} {fmt('tokens_before', tokens_before)} {fmt('tokens_after', tokens_after)} "
                f"{fmt('history_before', len(recent))} {fmt('history_after', len(history))} {fmt('budget', prompt_budget)} {fmt('correlation', correlation_id)}"
            )
        except Exception:
            pass

        # Generate with LLM if available; else fallback to placeholder
        reply = None
        if self.llm is not None:
            models_to_try: list[str] = []
            cfg_models = self.model_cfg.get("models")
            if isinstance(cfg_models, str):
                models_to_try = [m.strip() for m in cfg_models.split(",") if m.strip()]
            elif isinstance(cfg_models, list):
                models_to_try = [str(m) for m in cfg_models if str(m).strip()]
            if not models_to_try:
                try:
                    self.log.error(
                        f"llm-no-models-configured {fmt('channel', event.get('channel_name', event['channel_id']))} "
                        f"{fmt('user', event.get('author_name'))} {fmt('correlation', correlation_id)}"
                    )
                except Exception:
                    pass
                allow_auto = False
            else:
                allow_auto = bool(self.model_cfg.get("allow_auto_fallback", False))
            stops = self.model_cfg.get("stop")

            for idx, model_name in enumerate(models_to_try):
                if not model_name:
                    continue
                try:
                    start_ts = datetime.now(timezone.utc)
                    self.log.info(
                        f"llm-start "
                        f"{fmt('channel', event.get('channel_name', event['channel_id']))} "
                        f"{fmt('user', event.get('author_name'))} "
                        f"{fmt('model', model_name)} "
                        f"{fmt('fallback_index', idx)} "
                        f"{fmt('msg', getattr(message, 'id', ''))} "
                        f"{fmt('correlation', correlation_id)}"
                    )
                    result = await self.llm.generate_chat(
                        messages_for_est,
                        max_tokens=self.model_cfg.get("max_tokens"),
                        model=model_name,
                        temperature=self.model_cfg.get("temperature"),
                        top_p=self.model_cfg.get("top_p"),
                        stop=stops,
                        context_fields={
                            "channel": event.get('channel_name', event['channel_id']),
                            "user": event.get('author_name'),
                            "correlation": correlation_id,
                        },
                    )
                    reply = result.get("text") if isinstance(result, dict) else result
                    # Optional prompt/response logging to files
                    try:
                        from .config_service import ConfigService
                        cfg = ConfigService("config.yaml")
                        if bool(cfg.log_prompts()):
                            ts_dir = dt.now().strftime("prompts-%Y%m%d-%H%M%S")
                            out_dir = Path("logs") / ts_dir
                            out_dir.mkdir(parents=True, exist_ok=True)
                            import json
                            p = {
                                "correlation": correlation_id,
                                "channel": event.get('channel_name', event['channel_id']),
                                "user": event.get('author_name'),
                                "model": model_name,
                                "messages": messages_for_est,
                            }
                            (out_dir / "prompt.json").write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
                            lines = []
                            for m in messages_for_est:
                                lines.append(f"[{m.get('role','')}] {m.get('content','')}")
                            (out_dir / "prompt.txt").write_text("\n\n".join(lines), encoding="utf-8")
                            (out_dir / "response.txt").write_text(str(reply or ""), encoding="utf-8")
                    except Exception:
                        pass
                    # Strip a leading bracketed bot-name tag if the model echoed it (e.g., "[Assistant] ...")
                    try:
                        bot_display = None
                        if message.client and message.client.user:
                            bot_display = getattr(message.client.user, "display_name", None) or getattr(message.client.user, "name", None)
                        if isinstance(reply, str) and bot_display:
                            m = re.match(r"^\s*\[([^\]]+)\]\s*", reply)
                            if m and (bot_display.lower() in m.group(1).lower()):
                                reply = reply[m.end():]
                    except Exception:
                        pass
                    # If the model returned no usable text, log at ERROR so it is captured in errors.log
                    try:
                        if not isinstance(reply, str):
                            self.log.error(
                                f"llm-bad-response-type {fmt('channel', event.get('channel_name', event['channel_id']))} "
                                f"{fmt('user', event.get('author_name'))} {fmt('model', model_name)} "
                                f"{fmt('type', type(reply).__name__)} {fmt('correlation', correlation_id)}"
                            )
                            reply = ""
                        elif reply.strip() == "":
                            self.log.error(
                                f"llm-empty-response {fmt('channel', event.get('channel_name', event['channel_id']))} "
                                f"{fmt('user', event.get('author_name'))} {fmt('model', model_name)} "
                                f"{fmt('correlation', correlation_id)}"
                            )
                    except Exception:
                        pass
                    dur_ms = int((datetime.now(timezone.utc) - start_ts).total_seconds() * 1000)
                    usage = (result or {}).get("usage") if isinstance(result, dict) else None
                    if usage and (usage.get("input_tokens") is not None or usage.get("output_tokens") is not None):
                        self.log.info(
                            f"llm-finish "
                            f"{fmt('channel', event.get('channel_name', event['channel_id']))} "
                            f"{fmt('user', event.get('author_name'))} "
                            f"{fmt('model', model_name)} "
                            f"{fmt('duration_ms', dur_ms)} "
                            f"{fmt('tokens_in', usage.get('input_tokens','NA'))} "
                            f"{fmt('tokens_out', usage.get('output_tokens','NA'))} "
                            f"{fmt('total_tokens', usage.get('total_tokens','NA'))} "
                            f"{fmt('fallback_index', idx)} "
                            f"{fmt('correlation', correlation_id)}"
                        )
                    else:
                        self.log.info(
                            f"llm-finish "
                            f"{fmt('channel', event.get('channel_name', event['channel_id']))} "
                            f"{fmt('user', event.get('author_name'))} "
                            f"{fmt('model', model_name)} "
                            f"{fmt('duration_ms', dur_ms)} "
                            f"{fmt('fallback_index', idx)} "
                            f"{fmt('correlation', correlation_id)}"
                        )
                    # FULL: log request/response payloads (redacted scope)
                    try:
                        from .logger_factory import is_full_enabled
                        if is_full_enabled():
                            self.log.info(
                                f"payload-in user_msg={user_msg['content'][:500]} history_count={len(history)} correlation={correlation_id}"
                            )
                            if reply:
                                self.log.info(
                                    f"payload-out reply={reply[:1000]} correlation={correlation_id}"
                                )
                    except Exception:
                        pass
                    break
                except Exception as e:
                    self.log.error(f"LLM error with {model_name}: {e}")
                    reply = None
                    # Explicit marker that this model is exhausted before moving to next fallback
                    try:
                        self.log.error(
                            f"llm-model-exhausted {fmt('model', model_name)} {fmt('fallback_index', idx)} {fmt('correlation', correlation_id)}"
                        )
                    except Exception:
                        pass

            if reply is None and allow_auto:
                try:
                    # Note: all configured models failed; escalate to auto fallback
                    try:
                        self.log.error(
                            f"llm-fallback-start {fmt('model', 'openrouter/auto')} {fmt('correlation', correlation_id)}"
                        )
                    except Exception:
                        pass
                    self.log.info(
                        f"llm-autofallback "
                        f"{fmt('channel', event.get('channel_name', event['channel_id']))} "
                        f"{fmt('user', event.get('author_name'))} "
                        f"{fmt('correlation', correlation_id)}"
                    )
                    start_ts = datetime.now(timezone.utc)
                    result = await self.llm.generate_chat(
                        messages_for_est,
                        max_tokens=self.model_cfg.get("max_tokens"),
                        model="openrouter/auto",
                        temperature=self.model_cfg.get("temperature"),
                        top_p=self.model_cfg.get("top_p"),
                        stop=stops,
                        context_fields={
                            "channel": event.get('channel_name', event['channel_id']),
                            "user": event.get('author_name'),
                            "correlation": correlation_id,
                        },
                    )
                    reply = result.get("text") if isinstance(result, dict) else result
                    # Strip a leading bracketed bot-name tag if present
                    try:
                        bot_display = None
                        if message.client and message.client.user:
                            bot_display = getattr(message.client.user, "display_name", None) or getattr(message.client.user, "name", None)
                        if isinstance(reply, str) and bot_display:
                            m = re.match(r"^\s*\[([^\]]+)\]\s*", reply)
                            if m and (bot_display.lower() in m.group(1).lower()):
                                reply = reply[m.end():]
                    except Exception:
                        pass
                    # Empty/non-string response logging (auto-fallback)
                    try:
                        if not isinstance(reply, str):
                            self.log.error(
                                f"llm-bad-response-type {fmt('channel', event.get('channel_name', event['channel_id']))} "
                                f"{fmt('user', event.get('author_name'))} {fmt('model', 'openrouter/auto')} "
                                f"{fmt('type', type(reply).__name__)} {fmt('correlation', correlation_id)}"
                            )
                            reply = ""
                        elif reply.strip() == "":
                            self.log.error(
                                f"llm-empty-response {fmt('channel', event.get('channel_name', event['channel_id']))} "
                                f"{fmt('user', event.get('author_name'))} {fmt('model', 'openrouter/auto')} "
                                f"{fmt('correlation', correlation_id)}"
                            )
                    except Exception:
                        pass
                    dur_ms = int((datetime.now(timezone.utc) - start_ts).total_seconds() * 1000)
                    usage = (result or {}).get("usage") if isinstance(result, dict) else None
                    if usage and (usage.get("input_tokens") is not None or usage.get("output_tokens") is not None):
                        self.log.info(
                            f"llm-finish "
                            f"{fmt('channel', event.get('channel_name', event['channel_id']))} "
                            f"{fmt('user', event.get('author_name'))} "
                            f"{fmt('model', 'openrouter/auto')} "
                            f"{fmt('duration_ms', dur_ms)} "
                            f"{fmt('tokens_in', usage.get('input_tokens','NA'))} "
                            f"{fmt('tokens_out', usage.get('output_tokens','NA'))} "
                            f"{fmt('total_tokens', usage.get('total_tokens','NA'))} "
                            f"{fmt('correlation', correlation_id)}"
                        )
                    else:
                        self.log.info(
                            f"llm-finish "
                            f"{fmt('channel', event.get('channel_name', event['channel_id']))} "
                            f"{fmt('user', event.get('author_name'))} "
                            f"{fmt('model', 'openrouter/auto')} "
                            f"{fmt('duration_ms', dur_ms)} "
                            f"{fmt('correlation', correlation_id)}"
                        )
                except Exception as e2:
                    self.log.error(f"LLM auto fallback error: {e2}")
        if not reply:
            # Log as error before substituting a placeholder so it is captured in errors.log
            try:
                self.log.error(
                    f"llm-no-reply {fmt('channel', event.get('channel_name', event['channel_id']))} "
                    f"{fmt('user', event.get('author_name'))} {fmt('correlation', correlation_id)}"
                )
            except Exception:
                pass
            reply = f"(placeholder) You said: {event['content'][:200]}"

        # Send reply respecting Discord char limits; if too long even after allowed splits, attach as file
        try:
            from .config_service import ConfigService
            cfg = ConfigService("config.yaml")
            limit = max(1, int(cfg.discord_message_char_limit()))
            max_parts = max(1, int(cfg.max_response_messages()))
        except Exception:
            limit = 2000
            max_parts = 2
        cont_marker = " ....."
        lead_marker = "..... "
        max_conveyable = (limit - len(cont_marker)) + max(0, max_parts - 1) * (limit - len(lead_marker))

        sent = None
        if isinstance(reply, str) and len(reply) > max_conveyable:
            # Attach as file to avoid exceeding API limits; include a short notice
            buf = io.BytesIO(reply.encode("utf-8"))
            try:
                if decision.get("style") == "reply":
                    sent = await message.reply(content="(Response too long so has been attached as file.)", file=discord.File(fp=buf, filename="response.txt"))
                else:
                    sent = await message.channel.send(content="(Response too long so has been attached as file.)", file=discord.File(fp=buf, filename="response.txt"))
            except Exception:
                # Fallback: send only the first chunk with an explicit truncation notice
                parts = self._split_for_discord(reply)
                first = parts[0] if parts else ""
                # Build a '(Response Truncated)' marker while honoring the limit
                try:
                    from .config_service import ConfigService
                    cfg = ConfigService("config.yaml")
                    limit = max(1, int(cfg.discord_message_char_limit()))
                except Exception:
                    limit = 2000
                cont_marker = " ....."
                trunc_marker = " ..... (Response Truncated)"
                if first.endswith(cont_marker):
                    base = first[: -len(cont_marker)]
                else:
                    base = first
                # Ensure final fits within limit
                room = limit - len(trunc_marker)
                if room < 0:
                    room = 0
                base = base[:room].rstrip()
                final_chunk = (base + trunc_marker)[:limit]
                if decision.get("style") == "reply":
                    sent = await message.reply(final_chunk)
                else:
                    sent = await message.channel.send(final_chunk)
        else:
            parts = self._split_for_discord(reply)
            for idx, chunk in enumerate(parts):
                if decision.get("style") == "reply" and idx == 0:
                    sent = await message.reply(chunk)
                else:
                    sent = await message.channel.send(chunk)

        # Record assistant message into memory for future context
        skip_cooldown = False
        try:
            cm = getattr(self.policy, "conversation_mode", {})
            # If this decision came from conversation-mode and affects_cooldown is False, don't reset cooldown
            if decision.get("reason") == "conversation-mode" and cm and (cm.get("affects_cooldown") is False):
                skip_cooldown = True
        except Exception:
            pass
        if not skip_cooldown:
            self.memory.on_replied(event)
        # Start/refresh conversation mode window after a successful reply
        try:
            cm = getattr(self.policy, "conversation_mode", None)
            if cm and cm.get("enabled"):
                # Only start a window if one is not already active; do not reset/extend on each reply
                if not self.memory.conversation_mode_active(str(message.channel.id)):
                    self.memory.start_conversation_mode(
                        channel_id=str(message.channel.id),
                        window_seconds=int(cm.get("window_seconds", 120)),
                        max_messages=int(cm.get("max_messages", 5)),
                    )
                    self.log.info(
                        f"conversation-mode start channel={event.get('channel_name', event['channel_id'])} "
                        f"window_seconds={int(cm.get('window_seconds', 120))} max_messages={int(cm.get('max_messages', 5))} correlation={correlation_id}"
                    )
        except Exception:
            pass
        self.memory.record({
            "channel_id": str(message.channel.id),
            "author_id": str(getattr(getattr(sent, 'author', None), 'id', '0')) if sent else '0',
            "content": reply,
            "is_bot": True,
            "created_at": (sent.created_at.replace(tzinfo=timezone.utc) if (sent and getattr(sent, 'created_at', None)) else datetime.now(timezone.utc)),
            "author_name": (getattr(getattr(sent, 'author', None), 'display_name', 'bot') if sent else 'bot'),
        })

    async def build_batch_reply(self, cid: str, events: list[dict]) -> str | None:
        # Produce a single summarized reply for a batch of events; does NOT send it.
        if not events:
            return None
        if not self.llm:
            return None
        try:
            channel_id = cid
            # Consume one conversation message budget up front; if not possible, skip
            if not self.memory.consume_conversation_message(channel_id):
                return None
            # Summarize batch into a composite user message
            lines = []
            for e in events:
                author = e.get("author_name") or "user"
                content = e.get("content") or ""
                lines.append(f"[{author}] {content}")
            batch_text = "\n".join(lines)
            system_msg = {"role": "system", "content": self.tmpl.build_system_message()}
            recent = self.memory.get_recent(channel_id, limit=self.policy.window_size())
            # Collect structured messages
            structured_msgs = []
            for it in recent:
                role = "assistant" if it.get("is_bot") else "user"
                author = it.get("author_name") or ("bot" if it.get("is_bot") else "user")
                content = it.get("content", "")
                timestamp_iso = it.get("created_at").isoformat() if it.get("created_at") else None
                structured_msgs.append({"role": role, "author": author, "content": content, "timestamp_iso": timestamp_iso})
            # Re-cluster recent context similarly for batch prompts
            try:
                from .config_service import ConfigService
                cfg = ConfigService("config.yaml")
                recency_min = int(cfg.recency_minutes())
                cluster_max = max(1, int(cfg.cluster_max_messages()))
                thread_max = max(0, int(cfg.thread_affinity_max()))
                now_ts = datetime.now(timezone.utc)
                cutoff = now_ts - _timedelta(minutes=recency_min)
                def _parse_iso(ts: str | None):
                    if not ts:
                        return None
                    try:
                        t = dt.fromisoformat(ts)
                        if t.tzinfo is None:
                            t = t.replace(tzinfo=timezone.utc)
                        return t
                    except Exception:
                        return None
                recent_structs = []
                for m in structured_msgs:
                    t = _parse_iso(m.get("timestamp_iso"))
                    if (t is None) or (t >= cutoff):
                        recent_structs.append((t, m))
                if recent_structs:
                    recent_structs.sort(key=lambda x: (x[0] or now_ts))
                    clustered = [m for _, m in recent_structs][-cluster_max:]
                    structured_msgs = clustered
                else:
                    structured_msgs = structured_msgs[-cluster_max:]
                # Thread affinity for batch: prefer recent assistant turns and any turns from most frequent recent author
                try:
                    # Identify dominant recent author in the batch events
                    authors = [e.get("author_name") for e in events if e.get("author_name")]
                    dom_author = None
                    if authors:
                        from collections import Counter
                        dom_author = Counter(authors).most_common(1)[0][0]
                    tail_candidates = []
                    for m in reversed([m for _, m in recent_structs] if recent_structs else structured_msgs):
                        if m.get("role") == "assistant" or (dom_author and (m.get("role") == "user" and m.get("author") == dom_author)):
                            tail_candidates.append(m)
                        if len(tail_candidates) >= thread_max:
                            break
                    tail_candidates = list(reversed(tail_candidates))
                    seen = set()
                    merged = []
                    for m in structured_msgs + tail_candidates:
                        key = (m.get("role"), m.get("author"), m.get("content"))
                        if key in seen:
                            continue
                        seen.add(key)
                        merged.append(m)
                    structured_msgs = merged[-cluster_max:]
                except Exception:
                    pass
            except Exception:
                pass

            # Build raw history tail
            use_tmpl = False
            keep_tail = 2
            try:
                from .config_service import ConfigService
                cfg = ConfigService("config.yaml")
                use_tmpl = bool(cfg.use_template())
                keep_tail = int(cfg.keep_history_tail())
            except Exception:
                pass
            history = []
            if use_tmpl:
                tail = structured_msgs[-keep_tail:] if keep_tail > 0 else []
                for m in tail:
                    history.append({"role": m["role"], "content": m["content"]})
            else:
                for it in recent:
                    role = "assistant" if it.get("is_bot") else "user"
                    if it.get("is_bot"):
                        content = it.get("content", "")
                    else:
                        author = it.get("author_name") or "user"
                        content = f"[{author}] {it.get('content', '')}"
                    history.append({"role": role, "content": content})

            # If any event is a reply to the bot, ensure parent bot message is present
            try:
                parent_content = None
                for e in events:
                    if e.get("is_reply_to_bot") and e.get("reply_to_message_content"):
                        parent_content = e.get("reply_to_message_content")
                if parent_content and all(h.get("content") != parent_content for h in history):
                    history.append({"role": "assistant", "content": parent_content})
            except Exception:
                pass

            user_msg = {"role": "user", "content": batch_text}

            # Apply heuristic budgeting (same as single-message path)
            try:
                from .config_service import ConfigService
                cfg = ConfigService("config.yaml")
                max_ctx = int(cfg.max_context_tokens())
                reserve = int(cfg.response_tokens_max())
            except Exception:
                max_ctx = 8192
                reserve = 512
            prompt_budget = max(1, max_ctx - reserve)

            # Lore block (after system, before context)
            system_ctx = None
            builder = getattr(self.lore, "build_lore_block", None) if getattr(self, "lore", None) is not None else None
            messages_for_est = [system_msg]
            if builder and self.lore_cfg.get("enabled"):
                lore_fraction = float(self.lore_cfg.get("max_fraction", 0.33))
                prompt_budget = max(1, max_ctx - reserve)
                lore_budget = max(1, int(prompt_budget * lore_fraction))
                corpus_parts = [f"{m.get('author')}: {m.get('content')}" for m in structured_msgs]
                corpus_parts.append(batch_text)
                corpus = "\n".join(corpus_parts)
                lore_text = builder(corpus_text=corpus, max_tokens=lore_budget, tokenizer=self.tok, logger=self.log)
                if lore_text:
                    messages_for_est.append({"role": "system", "content": "You may use the following background context if it is relevant to the user’s request.\n" + lore_text})
            # Subtle current local time hint (no location/zone)
            try:
                messages_for_est.append({"role": "system", "content": f"Current Date/Time: {datetime.now().strftime('%Y-%m-%d %H:%M')}"})
            except Exception:
                pass
            if use_tmpl:
                context_block = self.tmpl.render(conversation_window=structured_msgs, user_input=batch_text, summary=None)
                system_ctx = {"role": "system", "content": context_block}
                messages_for_est.append(system_ctx)
            def _assemble():
                return [*messages_for_est, *history, user_msg]
            messages_for_est = _assemble()

            tokens_before = self.tok.estimate_tokens_messages(messages_for_est)
            if tokens_before > prompt_budget and history:
                trimmed = list(history)
                while trimmed and self.tok.estimate_tokens_messages([system_msg, *( [system_ctx] if system_ctx else [] ), *trimmed, user_msg]) > prompt_budget:
                    trimmed.pop(0)
                history = trimmed
                messages_for_est = _assemble()
            if self.tok.estimate_tokens_messages(messages_for_est) > prompt_budget:
                # truncate user content last
                user_msg["content"] = self.tok.truncate_text_tokens(user_msg["content"], max_tokens=max(8, prompt_budget - self.tok.estimate_tokens_messages([system_msg, *( [system_ctx] if system_ctx else [] ), *history])))
                messages_for_est = _assemble()
            try:
                tokens_after = self.tok.estimate_tokens_messages(messages_for_est)
                self.log.debug(
                    f"tokenizer-summary {fmt('channel', cid)} {fmt('user', 'batch')} "
                    f"{fmt('tokens_before', tokens_before)} {fmt('tokens_after', tokens_after)} {fmt('budget', prompt_budget)}"
                )
            except Exception:
                pass
            models_to_try: list[str] = []
            cfg_models = self.model_cfg.get("models")
            if isinstance(cfg_models, str):
                models_to_try = [m.strip() for m in cfg_models.split(",") if m.strip()]
            elif isinstance(cfg_models, list):
                models_to_try = [str(m) for m in cfg_models if str(m).strip()]
            allow_auto = bool(self.model_cfg.get("allow_auto_fallback", False))
            stops = self.model_cfg.get("stop")

            reply = None
            correlation_id = f"{cid}-batch"
            for idx, model_name in enumerate(models_to_try):
                if not model_name:
                    continue
                try:
                    start_ts = datetime.now(timezone.utc)
                    self.log.info(
                        f"llm-start {fmt('channel', cid)} {fmt('user', 'batch')} {fmt('model', model_name)} {fmt('fallback_index', idx)} {fmt('correlation', correlation_id)}"
                    )
                    result = await self.llm.generate_chat(
                        messages_for_est,
                        max_tokens=self.model_cfg.get("max_tokens"),
                        model=model_name,
                        temperature=self.model_cfg.get("temperature"),
                        top_p=self.model_cfg.get("top_p"),
                        stop=stops,
                        context_fields={"channel": cid, "user": "batch", "correlation": correlation_id},
                    )
                    reply = result.get("text") if isinstance(result, dict) else result
                    # Optional logging for batch prompts
                    try:
                        from .config_service import ConfigService
                        cfg = ConfigService("config.yaml")
                        if bool(cfg.log_prompts()):
                            ts_dir = dt.now().strftime("prompts-%Y%m%d-%H%M%S")
                            out_dir = Path("logs") / ts_dir
                            out_dir.mkdir(parents=True, exist_ok=True)
                            import json
                            p = {
                                "correlation": correlation_id,
                                "channel": cid,
                                "user": "batch",
                                "model": model_name,
                                "messages": messages_for_est,
                            }
                            (out_dir / "prompt.json").write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
                            lines = []
                            for m in messages_for_est:
                                lines.append(f"[{m.get('role','')}] {m.get('content','')}")
                            (out_dir / "prompt.txt").write_text("\n\n".join(lines), encoding="utf-8")
                            (out_dir / "response.txt").write_text(str(reply or ""), encoding="utf-8")
                    except Exception:
                        pass
                    dur_ms = int((datetime.now(timezone.utc) - start_ts).total_seconds() * 1000)
                    usage = (result or {}).get("usage") if isinstance(result, dict) else None
                    if usage and (usage.get("input_tokens") is not None or usage.get("output_tokens") is not None):
                        self.log.info(
                            f"llm-finish {fmt('channel', cid)} {fmt('user', 'batch')} {fmt('model', model_name)} {fmt('duration_ms', dur_ms)} "
                            f"{fmt('tokens_in', usage.get('input_tokens','NA'))} {fmt('tokens_out', usage.get('output_tokens','NA'))} {fmt('total_tokens', usage.get('total_tokens','NA'))} "
                            f"{fmt('fallback_index', idx)} {fmt('correlation', correlation_id)}"
                        )
                    else:
                        self.log.info(
                            f"llm-finish {fmt('channel', cid)} {fmt('user', 'batch')} {fmt('model', model_name)} {fmt('duration_ms', dur_ms)} {fmt('fallback_index', idx)} {fmt('correlation', correlation_id)}"
                        )
                    break
                except Exception as e:
                    self.log.error(f"LLM error with {model_name}: {e}")
                    reply = None
                    try:
                        self.log.error(f"llm-model-exhausted {fmt('model', model_name)} {fmt('fallback_index', idx)} {fmt('correlation', correlation_id)}")
                    except Exception:
                        pass

            if reply is None and allow_auto:
                try:
                    try:
                        self.log.error(f"llm-fallback-start {fmt('model', 'openrouter/auto')} {fmt('correlation', correlation_id)}")
                    except Exception:
                        pass
                    self.log.info(f"llm-autofallback {fmt('channel', cid)} {fmt('user', 'batch')} {fmt('correlation', correlation_id)}")
                    start_ts = datetime.now(timezone.utc)
                    result = await self.llm.generate_chat(
                        messages_for_est,
                        max_tokens=self.model_cfg.get("max_tokens"),
                        model="openrouter/auto",
                        temperature=self.model_cfg.get("temperature"),
                        top_p=self.model_cfg.get("top_p"),
                        stop=stops,
                        context_fields={"channel": cid, "user": "batch", "correlation": correlation_id},
                    )
                    reply = result.get("text") if isinstance(result, dict) else result
                    dur_ms = int((datetime.now(timezone.utc) - start_ts).total_seconds() * 1000)
                    usage = (result or {}).get("usage") if isinstance(result, dict) else None
                    if usage and (usage.get("input_tokens") is not None or usage.get("output_tokens") is not None):
                        self.log.info(
                            f"llm-finish {fmt('channel', cid)} {fmt('user', 'batch')} {fmt('model', 'openrouter/auto')} {fmt('duration_ms', dur_ms)} "
                            f"{fmt('tokens_in', usage.get('input_tokens','NA'))} {fmt('tokens_out', usage.get('output_tokens','NA'))} {fmt('total_tokens', usage.get('total_tokens','NA'))} "
                            f"{fmt('correlation', correlation_id)}"
                        )
                    else:
                        self.log.info(
                            f"llm-finish {fmt('channel', cid)} {fmt('user', 'batch')} {fmt('model', 'openrouter/auto')} {fmt('duration_ms', dur_ms)} {fmt('correlation', correlation_id)}"
                        )
                except Exception as e2:
                    self.log.error(f"LLM auto fallback error: {e2}")

            return reply
        except Exception as e:  # noqa
            self.log.error(f"Batch handling error for channel {cid}: {e}")
            return None
