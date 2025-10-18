from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta as _timedelta
from pathlib import Path
from datetime import datetime as dt
import io
from .task_queue import PendingMention
import discord
from .utils.correlation import make_correlation_id
from .utils.logfmt import fmt
import re
from .utils.time_utils import ensure_local, format_local, now_local


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

    def _system_message_for_overrides(self, *, is_nsfw: bool, persona_override: dict | None) -> str:
        if not persona_override:
            return self.tmpl.build_system_message_for(is_nsfw=is_nsfw)
        try:
            system_text = None
            if is_nsfw:
                system_text = persona_override.get('system_prompt_nsfw_text') or persona_override.get('system_prompt_text')
            else:
                system_text = persona_override.get('system_prompt_text')
            persona_text = persona_override.get('persona_text') or ''
            if not system_text:
                return self.tmpl.build_system_message_for(is_nsfw=is_nsfw)
            combined = system_text.strip()
            if persona_text.strip():
                combined = f"{combined}\n\n[Persona]\n{persona_text.strip()}"
            return combined
        except Exception:
            return self.tmpl.build_system_message_for(is_nsfw=is_nsfw)

    def _infer_provider_for_model(self, model_name: str, fallback: str | None = None) -> str | None:
        name = (model_name or '').lower().strip()
        if not name:
            return fallback
        # Heuristics: colon suffixes and vendor-prefixed identifiers indicate OpenRouter routing
        if ':' in name:
            return 'openrouter'
        if '/' in name:
            vendor = name.split('/', 1)[0]
            if vendor and vendor not in {'local', 'internal'}:
                return 'openrouter'
        if name.startswith('gpt-') or name.startswith('o1') or name.startswith('o3'):
            return 'openai'
        return fallback

    # ------------------------------------------------------------------
    # Shared prompt assembly + budgeting helper (single-turn & batch)
    # ------------------------------------------------------------------
    def _assemble_and_budget(self,
                              system_blocks: list[dict],
                              history: list[dict],
                              user_msg: dict,
                              prompt_budget: int,
                              protect_last_assistant: str | None = None,
                              truncate_user_min: int = 8) -> tuple[list[dict], int, int]:
        """Return (messages, tokens_before, tokens_after) applying trimming rules.

        Trimming strategy:
        1. Start with system_blocks + history + user.
        2. If over budget, drop oldest history entries (preserving the last assistant message whose
           content exactly matches protect_last_assistant, if provided) until within or history empty.
        3. If still over, truncate user content.
        """
        messages = [*system_blocks, *history, user_msg]
        try:
            tokens_before = self.tok.estimate_tokens_messages(messages)
        except Exception:
            tokens_before = -1
        # Step 1: trim history
        if tokens_before > prompt_budget and history:
            trimmed = list(history)
            def current_tokens(hlist):
                try:
                    return self.tok.estimate_tokens_messages([*system_blocks, *hlist, user_msg])
                except Exception:
                    return 10**9
            while trimmed and current_tokens(trimmed) > prompt_budget:
                # Avoid dropping protected last assistant content
                if protect_last_assistant and trimmed[0].get('role') == 'assistant' and trimmed[0].get('content') == protect_last_assistant:
                    if len(trimmed) > 1:
                        trimmed.pop(1)
                    else:
                        break
                else:
                    trimmed.pop(0)
            history = trimmed
            messages = [*system_blocks, *history, user_msg]
        # Step 2: truncate user body
        try:
            total_after_history = self.tok.estimate_tokens_messages(messages)
        except Exception:
            total_after_history = -1
        if total_after_history > prompt_budget:
            try:
                # Compute remaining allowance = prompt_budget - tokens(system_blocks + trimmed history w/ empty user)
                try:
                    empty_user = dict(user_msg)
                    empty_user['content'] = ''
                    base_tokens = self.tok.estimate_tokens_messages([*system_blocks, *history, empty_user])
                except Exception:
                    base_tokens = 0
                remaining_raw = prompt_budget - base_tokens
                if remaining_raw < 1:
                    remaining_raw = 1
                # If overall budget is smaller than truncate_user_min, allow smaller output
                if prompt_budget < truncate_user_min:
                    remaining = remaining_raw
                else:
                    if remaining_raw < truncate_user_min:
                        remaining_raw = truncate_user_min
                    remaining = remaining_raw
                allow = remaining
                original = user_msg.get('content', '')
                truncated = self.tok.truncate_text_tokens(original, max_tokens=allow)
                # Fallback: if tokenizer's truncation did not reduce enough tokens (len words proxy), slice manually
                # Final guard: enforce <= remaining (allow)
                toks = truncated.split()
                if len(toks) > allow:
                    truncated = ' '.join(toks[:allow])
                user_msg['content'] = truncated
            except Exception:
                pass
            messages = [*system_blocks, *history, user_msg]
        try:
            tokens_after = self.tok.estimate_tokens_messages(messages)
        except Exception:
            tokens_after = -1
        return messages, tokens_before, tokens_after

    # ------------------------------------------------------------------
    # Small helpers to simplify handle_message
    # ------------------------------------------------------------------
    def _resolve_names(self, message) -> tuple[str, str]:
        try:
            channel_name = getattr(message.channel, "name", None) or str(message.channel.id)
        except Exception:
            channel_name = str(getattr(message.channel, "id", "unknown"))
        try:
            guild_name = getattr(message.guild, "name", None) or ("DM" if getattr(message.channel, "type", None) and str(message.channel.type).lower().endswith("dm") else "unknown")
        except Exception:
            guild_name = "unknown"
        return channel_name, guild_name

    def _get_bot_id(self, message):
        bot_id = None
        try:
            if message.client and message.client.user:
                bot_id = message.client.user.id
        except Exception:
            bot_id = getattr(getattr(message.guild, "me", None), "id", None)
        return bot_id

    def _populate_reply_metadata(self, event: dict, message, bot_id) -> None:
        event["is_reply"] = bool(message.reference and getattr(message.reference, "message_id", None))
        event["is_reply_to_bot"] = False
        if not event["is_reply"]:
            return
        try:
            ref = message.reference
            parent = getattr(ref, "resolved", None) or getattr(ref, "cached_message", None)
            if isinstance(parent, discord.Message):
                event["reply_to_author_id"] = str(parent.author.id)
                event["reply_to_is_bot"] = bool(getattr(parent.author, "bot", False))
                event["reply_to_message_content"] = parent.content or ""
                event["reply_to_author_name"] = getattr(parent.author, "display_name", None) or ("bot" if getattr(parent.author, "bot", False) else "user")
                if bot_id and parent.author.id == bot_id:
                    event["is_reply_to_bot"] = True
        except Exception:
            pass

    def _maybe_enqueue_for_batch(self, event: dict, message, is_direct: bool, allowed_channel: bool) -> bool:
        try:
            if self.batcher and self.memory.conversation_mode_active(event["channel_id"]) and not event.get("is_mentioned", False):
                if allowed_channel or event.get("is_reply"):
                    if not (is_direct or allowed_channel):
                        self.memory.record(event)
                    try:
                        added = self.batcher.add(event["channel_id"], event)
                        if added:
                            self.log.debug(
                                f"batch-enqueue {fmt('channel', event.get('channel_name', event['channel_id']))} {fmt('msg', event.get('message_id',''))}"
                            )
                        else:
                            self.log.debug(
                                f"batch-dedupe {fmt('channel', event.get('channel_name', event['channel_id']))} {fmt('msg', event.get('message_id',''))}"
                            )
                    except Exception:
                        pass
                    return True
        except Exception:
            pass
        return False

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
        channel_name, guild_name = self._resolve_names(message)
        event = {
            "channel_id": str(message.channel.id),
            "channel_name": channel_name,
            "author_id": str(message.author.id),
            "message_id": str(message.id),
            "content": message.content or "",
            "mentions": [m.id for m in message.mentions],
            "is_bot": bool(getattr(message.author, "bot", False)),
            "created_at": ensure_local(message.created_at),
            "author_name": message.author.display_name,
            "guild_name": guild_name,
        }

        # Determine bot user id
        bot_id = self._get_bot_id(message)

        # Mentions
        event["is_mentioned"] = bool(bot_id and any(mid == bot_id for mid in event["mentions"]))

        # Reply metadata
        self._populate_reply_metadata(event, message, bot_id)

        # Determine direct message trigger by name/mention
        content_lower = (event.get("content") or "").lower()
        name_matched = any(alias in content_lower for alias in self.policy.aliases) if getattr(self.policy, "respond_to_name", False) else False
        # Treat replies to the bot as direct triggers as well
        is_direct = bool(event.get("is_mentioned") or name_matched or event.get("is_reply_to_bot"))
        allowed_channel = event["channel_id"] in getattr(self.policy, "allowed_general_channels", set())

        # Record only if allowed channel for general chat OR it's a direct trigger
        if is_direct or allowed_channel:
            self.memory.record(event)
        if self._maybe_enqueue_for_batch(event, message, is_direct, allowed_channel):
            return

        # Correlation id ties decision → LLM call → send
        correlation_id = make_correlation_id(event['channel_id'], getattr(message, 'id', 'msg'))
        event["correlation"] = correlation_id

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
            f"[Decision] "
            f"{fmt('allow', allow_flag)} "
            f"{fmt('reason', decision.get('reason'))} "
            f"{fmt('channel', event.get('channel_name', event['channel_id']))} "
            f"{fmt('user', event.get('author_name'))} "
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
            raw_created = it.get("created_at")
            if isinstance(raw_created, datetime):
                created_local = ensure_local(raw_created)
            elif isinstance(raw_created, str):
                try:
                    created_local = ensure_local(datetime.fromisoformat(raw_created.replace("Z", "+00:00")))
                except Exception:
                    created_local = None
            else:
                created_local = None
            timestamp_iso = format_local(created_local) if created_local else None
            structured_msgs.append({"role": role, "author": author, "content": content, "timestamp_iso": timestamp_iso})
        # Re-cluster to prioritize the most recent conversation context
        try:
            from .config_service import ConfigService
            cfg = ConfigService("config.yaml")
            recency_min = int(cfg.recency_minutes())
            cluster_max = max(1, int(cfg.cluster_max_messages()))
            thread_max = max(0, int(cfg.thread_affinity_max()))
            now_ts = ensure_local(event.get("created_at")) or now_local()
            cutoff = now_ts - _timedelta(minutes=recency_min)
            def _parse_iso(ts: str | None):
                if not ts:
                    return None
                try:
                    normalized = ts.replace("Z", "+00:00")
                    return ensure_local(dt.fromisoformat(normalized))
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
        # Avoid duplication: if the most recent structured entry matches the current user message, drop it
        try:
            if structured_msgs:
                last = structured_msgs[-1]
                if last.get("role") == "user":
                    cur_author = event.get("author_name") or "user"
                    cur_content = event.get("content") or ""
                    if last.get("content") == cur_content and (last.get("author") == cur_author or not last.get("author")):
                        structured_msgs.pop()
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
        # Hot-apply lore path changes from config (rebuild LoreService if paths changed)
        builder = None
        try:
            if getattr(self, "lore", None) is not None:
                from .config_service import ConfigService
                cfg = ConfigService("config.yaml")
                new_paths = cfg.lore_paths()
                # Compare lists as strings for stability
                current_paths = getattr(self, "_lore_paths", None)
                if current_paths is None or [str(p) for p in new_paths] != [str(p) for p in (current_paths or [])]:
                    from .lore_service import LoreService as _Lore
                    self.lore = _Lore(new_paths, md_priority=cfg.lore_md_priority()) if cfg.lore_enabled() else None
                    self._lore_paths = list(new_paths)
                builder = getattr(self.lore, "build_lore_block", None) if self.lore is not None else None
        except Exception:
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
            messages_for_est.append({"role": "system", "content": f"Current Date/Time: {now_local().strftime('%Y-%m-%d %H:%M %z')}"})
        except Exception:
            pass

        # If using template, render a context block and attach as an extra system message (after lore)
        if use_tmpl:
            context_block = self.tmpl.render(conversation_window=structured_msgs, user_input=event['content'], summary=None)
            # Guard against accidental duplication: if the rendered block somehow contains
            # the base system/persona text, strip everything before the first context marker.
            try:
                if isinstance(context_block, str):
                    markers = ["[Conversation Summary]", "[Last User Message]", "[Recent Messages", "[Older Messages"]
                    idxs = [context_block.find(m) for m in markers if m in context_block]
                    first_idx = min(idxs) if idxs else -1
                    if first_idx > 0:
                        context_block = context_block[first_idx:]
            except Exception:
                pass
            if context_block:
                system_ctx = {"role": "system", "content": context_block}
                messages_for_est.append(system_ctx)

        # Vision single-pass: if enabled, scope allows, and images present, convert user content to multimodal parts
        has_images = False
        image_urls: list[str] = []
        vcfg = None
        user_content = user_msg["content"]  # may become a multimodal parts list
        try:
            from .config_service import ConfigService as _Cfg
            vcfg = _Cfg("config.yaml")
            if vcfg.vision_enabled():
                # Determine scope class for gating
                content_lower = (event.get("content") or "").lower()
                name_matched = any(a in content_lower for a in getattr(self.policy, "aliases", [])) if getattr(self.policy, "respond_to_name", False) else False
                if event.get("is_reply_to_bot"):
                    scope_class = "replies"
                elif event.get("is_mentioned") or name_matched:
                    scope_class = "mentions"
                else:
                    scope_class = "general_chat"
                apply = vcfg.vision_apply_in()
                if bool(apply.get(scope_class, False)):
                    try:
                        from .vision_utils import extract_image_urls
                        urls = extract_image_urls(message)
                        # Also inspect the referenced (parent) message for images when replying
                        try:
                            ref = getattr(message, "reference", None)
                            if ref and getattr(ref, "resolved", None):
                                parent = ref.resolved
                                if parent:
                                    urls_parent = extract_image_urls(parent)
                                    if urls_parent:
                                        urls.extend(u for u in urls_parent if u not in urls)
                        except Exception:
                            pass
                        try:
                            self.log.debug(
                                f"[vision-detect] {fmt('scope', scope_class)} {fmt('found', len(urls))}"
                            )
                        except Exception:
                            pass
                    except Exception:
                        urls = []
                    if urls:
                        max_imgs = int(vcfg.vision_max_images())
                        image_urls = urls[:max_imgs]
                        # Build content parts: first text, then images (use append to avoid narrow type inference)
                        parts = []
                        parts.append({"type": "text", "text": user_msg["content"]})
                        for u in image_urls:
                            parts.append({"type": "image_url", "image_url": {"url": u}})
                        user_content = parts
                        has_images = True
        except Exception:
            pass

        # Assemble + budget via shared helper
        final_user_msg = {"role": "user", "content": user_content}
        system_blocks = list(messages_for_est)  # system + lore + time + template (if any)
        protect = event.get("reply_to_message_content") if event.get("is_reply_to_bot") else None
        messages_for_est, tokens_before, tokens_after = self._assemble_and_budget(
            system_blocks=system_blocks,
            history=history,
            user_msg=final_user_msg,
            prompt_budget=prompt_budget,
            protect_last_assistant=protect,
            truncate_user_min=8,
        )
        try:
            self.log.debug(
                f"[tokenizer-summary] {fmt('channel', event.get('channel_name', event['channel_id']))} "
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
                        f"[llm-no-models-configured] {fmt('channel', event.get('channel_name', event['channel_id']))} "
                        f"{fmt('user', event.get('author_name'))} {fmt('correlation', correlation_id)}"
                    )
                except Exception:
                    pass
                allow_auto = False
            else:
                allow_auto = bool(self.model_cfg.get("allow_auto_fallback", False))
            stops = self.model_cfg.get("stop")

            # Prefer vision-capable models when sending images
            try:
                if has_images and vcfg and vcfg.vision_models():
                    vmods = [str(m) for m in vcfg.vision_models() if str(m).strip()]
                    if vmods:
                        models_to_try = vmods
            except Exception:
                pass

            # Determine provider list for this context if available
            _nsfw_flag_detect = False
            try:
                ch = getattr(message, 'channel', None)
                if ch is not None:
                    _nsfw_flag_detect = bool(getattr(ch, 'nsfw', False)) or bool(getattr(getattr(ch, 'parent', None), 'nsfw', False))
            except Exception:
                _nsfw_flag_detect = False
            base_cf = {
                "channel": event.get('channel_name', event['channel_id']),
                "user": event.get('author_name'),
                "correlation": correlation_id,
                "nsfw": _nsfw_flag_detect,
                "has_images": has_images,
            }
            # Propagate web flag if set on the event (web router sets this)
            try:
                if bool(event.get('web')):
                    base_cf['web'] = True
            except Exception:
                pass
            provider_indices = [0]
            try:
                if hasattr(self.llm, 'providers_for_context'):
                    plist = self.llm.providers_for_context(base_cf)  # type: ignore[attr-defined]
                    if isinstance(plist, list) and len(plist) > 1:
                        provider_indices = list(range(len(plist)))
            except Exception:
                pass

            for p_index in provider_indices:
                for idx, model_name in enumerate(models_to_try):
                    if not model_name:
                        continue
                    start_ts = now_local()
                    line = (
                        f"[llm-start] "
                        f"{fmt('channel', event.get('channel_name', event['channel_id']))} "
                        f"{fmt('user', event.get('author_name'))} "
                        f"{fmt('model', model_name)} "
                        f"{fmt('nsfw', _nsfw_flag_detect)} "
                        f"{fmt('has_images', has_images)} "
                        + (f" {fmt('image_count', len(image_urls))}" if has_images else "") + " "
                        f"{fmt('fallback_index', idx)} "
                        f"{fmt('msg', getattr(message, 'id', ''))} "
                        f"{fmt('correlation', correlation_id)}"
                    )
                    # Demote router-level start to DEBUG; authoritative start is logged by the provider selector
                    self.log.debug(line)
                    cf_send = dict(base_cf)
                    cf_send["provider_index"] = p_index
                    try:
                        result = await self.llm.generate_chat(
                            messages_for_est,
                            max_tokens=self.model_cfg.get("max_tokens"),
                            model=model_name,
                            temperature=self.model_cfg.get("temperature"),
                            top_p=self.model_cfg.get("top_p"),
                            stop=stops,
                            context_fields=cf_send,
                        )
                    except Exception as e:
                        self.log.error(f"LLM error with {model_name}: {e}")
                        reply = None
                        # Explicit marker that this model is exhausted before moving to next fallback
                        try:
                            self.log.error(
                                f"[llm-model-exhausted] {fmt('model', model_name)} {fmt('fallback_index', idx)} {fmt('correlation', correlation_id)}"
                            )
                        except Exception:
                            pass
                        continue
                    reply = result.get("text") if isinstance(result, dict) else result
                    provider_used = (result or {}).get("provider") if isinstance(result, dict) else None
                    # Optional prompt/response logging to files
                    try:
                        from .config_service import ConfigService
                        cfg = ConfigService("config.yaml")
                        if bool(cfg.log_prompts()):
                            ts_dir = now_local().strftime("prompts-%Y%m%d-%H%M%S")
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
                                f"[llm-bad-response-type] {fmt('channel', event.get('channel_name', event['channel_id']))} "
                                f"{fmt('user', event.get('author_name'))} {fmt('model', model_name)} "
                                f"{fmt('type', type(reply).__name__)} {fmt('correlation', correlation_id)}"
                            )
                            reply = ""
                        elif reply.strip() == "":
                            self.log.error(
                                f"[llm-empty-response] {fmt('channel', event.get('channel_name', event['channel_id']))} "
                                f"{fmt('user', event.get('author_name'))} {fmt('model', model_name)} "
                                f"{fmt('correlation', correlation_id)}"
                            )
                    except Exception:
                        pass
                    dur_ms = int((now_local() - start_ts).total_seconds() * 1000)
                    usage = (result or {}).get("usage") if isinstance(result, dict) else None
                    # Normalize model label when using OpenAI-compatible backend
                    model_label = 'openai-compat' if (provider_used == 'openai') else model_name
                    if usage and (usage.get("input_tokens") is not None or usage.get("output_tokens") is not None):
                        self.log.info(
                            f"[llm-finish] "
                            f"{fmt('channel', event.get('channel_name', event['channel_id']))} "
                            f"{fmt('user', event.get('author_name'))} "
                            f"{fmt('model', model_label)} "
                            f"{fmt('provider', provider_used or 'unknown')} "
                            f"{fmt('duration_ms', dur_ms)} "
                            f"{fmt('nsfw', _nsfw_flag_detect)} "
                            f"{fmt('has_images', has_images)} "
                            + (f" {fmt('image_count', len(image_urls))}" if has_images else "") + " "
                            f"{fmt('tokens_in', usage.get('input_tokens','NA'))} "
                            f"{fmt('tokens_out', usage.get('output_tokens','NA'))} "
                            f"{fmt('total_tokens', usage.get('total_tokens','NA'))} "
                            f"{fmt('fallback_index', idx)} "
                            f"{fmt('correlation', correlation_id)}"
                        )
                    else:
                        self.log.info(
                            f"[llm-finish] "
                            f"{fmt('channel', event.get('channel_name', event['channel_id']))} "
                            f"{fmt('user', event.get('author_name'))} "
                            f"{fmt('model', model_label)} "
                            f"{fmt('provider', provider_used or 'unknown')} "
                            f"{fmt('duration_ms', dur_ms)} "
                            f"{fmt('nsfw', _nsfw_flag_detect)} "
                            f"{fmt('has_images', has_images)} "
                            + (f" {fmt('image_count', len(image_urls))}" if has_images else "") + " "
                            f"{fmt('fallback_index', idx)} "
                            f"{fmt('correlation', correlation_id)}"
                        )
                    # FULL: log request/response payloads (redacted scope)
                    try:
                        from .logger_factory import is_full_enabled
                        if is_full_enabled():
                            # Safe stringification; content may be multimodal parts
                            try:
                                preview = str(final_user_msg['content'])
                            except Exception:
                                preview = "<unprintable>"
                            self.log.info(
                                f"[payload-in] user_msg={preview[:500]} history_count={len(history)} correlation={correlation_id}"
                            )
                            if reply:
                                self.log.info(
                                    f"[payload-out] reply={reply[:1000]} correlation={correlation_id}"
                                )
                    except Exception:
                        pass
                    break
                if reply:
                    break

            if reply is None and allow_auto:
                try:
                    # Note: all configured models failed; escalate to auto fallback
                    try:
                        self.log.error(
                            f"[llm-fallback-start] {fmt('model', 'openrouter/auto')} {fmt('correlation', correlation_id)}"
                        )
                    except Exception:
                        pass
                    self.log.info(
                        f"[llm-autofallback] "
                        f"{fmt('channel', event.get('channel_name', event['channel_id']))} "
                        f"{fmt('user', event.get('author_name'))} "
                        f"{fmt('correlation', correlation_id)}"
                    )
                    start_ts = now_local()
                    # Compute NSFW flag safely for auto-fallback
                    try:
                        _nsfw_auto = False
                        ch = getattr(message, 'channel', None)
                        if ch is not None:
                            _nsfw_auto = bool(getattr(ch, 'nsfw', False)) or bool(getattr(getattr(ch, 'parent', None), 'nsfw', False))
                    except Exception:
                        _nsfw_auto = False
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
                            "nsfw": _nsfw_auto,
                            "has_images": has_images,
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
                                f"[llm-bad-response-type] {fmt('channel', event.get('channel_name', event['channel_id']))} "
                                f"{fmt('user', event.get('author_name'))} {fmt('model', 'openrouter/auto')} "
                                f"{fmt('type', type(reply).__name__)} {fmt('correlation', correlation_id)}"
                            )
                            reply = ""
                        elif reply.strip() == "":
                            self.log.error(
                                f"[llm-empty-response] {fmt('channel', event.get('channel_name', event['channel_id']))} "
                                f"{fmt('user', event.get('author_name'))} {fmt('model', 'openrouter/auto')} "
                                f"{fmt('correlation', correlation_id)}"
                            )
                    except Exception:
                        pass
                    dur_ms = int((now_local() - start_ts).total_seconds() * 1000)
                    usage = (result or {}).get("usage") if isinstance(result, dict) else None
                    if usage and (usage.get("input_tokens") is not None or usage.get("output_tokens") is not None):
                        self.log.info(
                            f"[llm-finish] "
                            f"{fmt('channel', event.get('channel_name', event['channel_id']))} "
                            f"{fmt('user', event.get('author_name'))} "
                            f"{fmt('model', 'openrouter/auto')} "
                            f"{fmt('duration_ms', dur_ms)} "
                            f"{fmt('has_images', has_images)} "
                            f"{fmt('image_count', len(image_urls) if has_images else 0)} "
                            f"{fmt('tokens_in', usage.get('input_tokens','NA'))} "
                            f"{fmt('tokens_out', usage.get('output_tokens','NA'))} "
                            f"{fmt('total_tokens', usage.get('total_tokens','NA'))} "
                            f"{fmt('correlation', correlation_id)}"
                        )
                    else:
                        self.log.info(
                            f"[llm-finish] "
                            f"{fmt('channel', event.get('channel_name', event['channel_id']))} "
                            f"{fmt('user', event.get('author_name'))} "
                            f"{fmt('model', 'openrouter/auto')} "
                            f"{fmt('duration_ms', dur_ms)} "
                            f"{fmt('has_images', has_images)} "
                            f"{fmt('image_count', len(image_urls) if has_images else 0)} "
                            f"{fmt('correlation', correlation_id)}"
                        )
                except Exception as e2:
                    self.log.error(f"LLM auto fallback error: {e2}")
        if not reply:
            # Log as error before substituting a placeholder so it is captured in errors.log
            try:
                self.log.error(
                    f"[llm-no-reply] {fmt('channel', event.get('channel_name', event['channel_id']))} "
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
                        f"[conversation-mode] [start] channel={event.get('channel_name', event['channel_id'])} "
                        f"window_seconds={int(cm.get('window_seconds', 120))} max_messages={int(cm.get('max_messages', 5))} correlation={correlation_id}"
                    )
        except Exception:
            pass
        self.memory.record({
            "channel_id": str(message.channel.id),
            "author_id": str(getattr(getattr(sent, 'author', None), 'id', '0')) if sent else '0',
            "content": reply,
            "is_bot": True,
            "created_at": (ensure_local(sent.created_at) if (sent and getattr(sent, 'created_at', None)) else now_local()),
            "author_name": (getattr(getattr(sent, 'author', None), 'display_name', 'bot') if sent else 'bot'),
        })

    async def build_batch_reply(self, cid: str, events: list[dict], channel=None, allow_outside_window: bool = False) -> str | None:
        """Produce a summarized reply for a batch of messages.

        cid: channel id (string)
        events: list of event dicts drained from the batcher
        channel: optional discord channel object (for NSFW system prompt selection)
        allow_outside_window: if True, skip consuming conversation-mode budget (used on window flush)
        """
        if not events or not self.llm:
            return None
        # Budget logic with override support
        if not allow_outside_window:
            is_override = False
            try:
                is_override = self.policy.is_response_chance_override(cid)
            except Exception:
                is_override = False
            if is_override:
                self.log.debug(f"[batch-override-no-consume] channel={cid} events={len(events)}")
            else:
                if not self.memory.consume_conversation_message(cid):
                    self.log.debug(f"[batch-skip] budget_exhausted channel={cid} events={len(events)}")
                    return None
        # Aggregate user batch text (content only) to reduce false matches on author names
        batch_text = "\n".join((e.get('content') or '') for e in events)
        override_payload: dict | None = None
        for e in events:
            if isinstance(e, dict):
                payload = e.get('web_overrides')
                if isinstance(payload, dict):
                    override_payload = payload
        persona_override = None
        lore_override_paths = None
        model_override = None
        if override_payload:
            maybe_persona = override_payload.get('persona')
            if isinstance(maybe_persona, dict):
                persona_override = maybe_persona
            maybe_lore = override_payload.get('lore_paths')
            if isinstance(maybe_lore, list):
                lore_override_paths = [str(p) for p in maybe_lore if p]
            maybe_model = override_payload.get('model')
            if isinstance(maybe_model, str) and maybe_model.strip():
                model_override = maybe_model.strip()
            try:
                self.log.info(
                    "[override-active] %s %s %s %s",
                    fmt('channel', cid),
                    fmt('persona', persona_override.get('name') if persona_override else 'default'),
                    fmt('model', model_override or 'default'),
                    fmt('lore_count', len(lore_override_paths) if lore_override_paths is not None else 'default'),
                )
            except Exception:
                pass
        # NSFW system selection
        is_nsfw = False
        try:
            if channel is not None:
                is_nsfw = bool(getattr(channel, 'nsfw', False)) or bool(getattr(getattr(channel, 'parent', None), 'nsfw', False))
        except Exception:
            pass
        system_msg = {"role": "system", "content": self._system_message_for_overrides(is_nsfw=is_nsfw, persona_override=persona_override)}
        # Config-driven parameters
        cfg = None
        try:
            from .config_service import ConfigService
            cfg = ConfigService('config.yaml')
            use_tmpl = bool(cfg.use_template())
            keep_tail = int(cfg.keep_history_tail())
            max_ctx = int(cfg.max_context_tokens())
            reserve = int(cfg.response_tokens_max())
        except Exception:
            use_tmpl, keep_tail, max_ctx, reserve = True, 2, 8192, 512
            cfg = None
        prompt_budget = max(1, max_ctx - reserve)
        # Recent context
        recent = self.memory.get_recent(cid, limit=self.policy.window_size())
        structured = []
        for it in recent:
            structured.append({
                'role': 'assistant' if it.get('is_bot') else 'user',
                'author': it.get('author_name') or ('bot' if it.get('is_bot') else 'user'),
                'content': it.get('content',''),
                'timestamp_iso': it.get('created_at').isoformat() if it.get('created_at') else None,
            })
        # History assembly (raw or template-tail)
        history: list[dict] = []
        # Avoid duplication: remove current batch user message if it mirrors the last structured entry
        try:
            if structured:
                last = structured[-1]
                if last.get('role') == 'user':
                    if (last.get('content') or '') == (batch_text or ''):
                        structured = structured[:-1]
        except Exception:
            pass
        if use_tmpl:
            tail = structured[-keep_tail:] if keep_tail > 0 else []
            for m in tail:
                history.append({'role': m['role'], 'content': m['content']})
        else:
            for it in recent:
                role = 'assistant' if it.get('is_bot') else 'user'
                if it.get('is_bot'):
                    content = it.get('content','')
                else:
                    author = it.get('author_name') or 'user'
                    content = f"[{author}] {it.get('content','')}"
                history.append({'role': role, 'content': content})
        # Preserve parent message when replying to bot
        parent_content = None
        try:
            for e in events:
                if e.get('is_reply_to_bot') and e.get('reply_to_message_content'):
                    parent_content = e.get('reply_to_message_content')
            if parent_content and all(h.get('content') != parent_content for h in history):
                history.append({'role': 'assistant', 'content': parent_content})
        except Exception:
            pass
        user_msg = {'role': 'user', 'content': batch_text}
        context_template_backup: str | None = None
        if persona_override and persona_override.get('context_template_text') is not None:
            try:
                context_template_backup = getattr(self.tmpl, 'context_template', None)
                self.tmpl.context_template = persona_override.get('context_template_text') or ''
            except Exception:
                context_template_backup = None
        persona_lore_default: list[str] = []
        base_lore_paths_default: list[str] = []
        if persona_override:
            persona_lore_default = persona_override.get('persona_lore_paths') or []
            base_lore_paths_default = persona_override.get('base_lore_paths') or []
        elif cfg:
            try:
                default_bundle = cfg.persona_bundle(cfg.persona_name()) or {}
                persona_lore_default = default_bundle.get('persona_lore_paths', []) or []
                base_lore_paths_default = default_bundle.get('base_lore_paths', []) or []
            except Exception:
                persona_lore_default = []
                base_lore_paths_default = []
        system_blocks = [system_msg]
        # Lore block
        builder = getattr(self.lore, 'build_lore_block', None) if getattr(self, 'lore', None) is not None else None
        if (persona_override is not None) or (lore_override_paths is not None):
            combined_paths: list[str] = []
            for p in base_lore_paths_default or []:
                if p:
                    combined_paths.append(p)
            for p in persona_lore_default or []:
                if p:
                    combined_paths.append(p)
            if lore_override_paths is not None:
                for p in lore_override_paths or []:
                    if p:
                        combined_paths.append(p)
            normalized_paths: list[str] = []
            for p in combined_paths:
                try:
                    normalized_paths.append(str(Path(p).resolve()))
                except Exception:
                    continue
            dedup_paths: list[str] = []
            seen: set[str] = set()
            for p in normalized_paths:
                if p not in seen:
                    seen.add(p)
                    dedup_paths.append(p)
            if dedup_paths:
                try:
                    from .lore_service import LoreService as _Lore
                    override_lore = _Lore(dedup_paths, md_priority=self.lore_cfg.get('md_priority', 'low'))
                    builder = getattr(override_lore, 'build_lore_block', None)
                    self.log.debug(
                        "[override-lore] %s %s %s",
                        fmt('channel', cid),
                        fmt('paths', len(dedup_paths)),
                        fmt('persona_paths', len(persona_lore_default or [])),
                    )
                except Exception:
                    builder = getattr(self.lore, 'build_lore_block', None) if getattr(self, 'lore', None) is not None else None
        if builder and self.lore_cfg.get('enabled'):
            try:
                lore_fraction = float(self.lore_cfg.get('max_fraction', 0.33))
                lore_budget = max(1, int(prompt_budget * lore_fraction))
                # Build corpus from message content only (exclude author names to avoid spurious key hits like 'You')
                corpus = "\n".join([m.get('content','') for m in structured] + [batch_text])
                lore_text = builder(corpus_text=corpus, max_tokens=lore_budget, tokenizer=self.tok, logger=self.log)
                if lore_text:
                    system_blocks.append({'role': 'system', 'content': 'You may use the following background context if it is relevant to the user’s request.\n' + lore_text})
            except Exception:
                pass
        # Time hint
        try:
            system_blocks.append({'role': 'system', 'content': f"Current Date/Time: {now_local().strftime('%Y-%m-%d %H:%M %z')}"})
        except Exception:
            pass
        # Template context
        if use_tmpl:
            try:
                context_block = self.tmpl.render(conversation_window=structured, user_input=batch_text, summary=None)
                # Guard against accidental duplication; trim any prefix before context markers
                if isinstance(context_block, str):
                    markers = ["[Conversation Summary]", "[Last User Message]", "[Recent Messages", "[Older Messages"]
                    idxs = [context_block.find(m) for m in markers if m in context_block]
                    first_idx = min(idxs) if idxs else -1
                    if first_idx > 0:
                        context_block = context_block[first_idx:]
                if context_block:
                    system_blocks.append({'role': 'system', 'content': context_block})
            except Exception:
                pass
        if context_template_backup is not None:
            try:
                self.tmpl.context_template = context_template_backup
            except Exception:
                pass
        # Budget using shared helper
        # Ensure parent_content variable exists even if earlier try block failed
        if 'parent_content' not in locals():
            parent_content = None
        messages, tokens_before, tokens_after = self._assemble_and_budget(
            system_blocks=system_blocks,
            history=history,
            user_msg=user_msg,
            prompt_budget=prompt_budget,
            protect_last_assistant=parent_content,
            truncate_user_min=8,
        )
        try:
            self.log.debug(
                f"[tokenizer-summary] {fmt('channel', cid)} {fmt('user', 'batch')} {fmt('tokens_before', tokens_before)} {fmt('tokens_after', tokens_after)} {fmt('budget', prompt_budget)}"
            )
        except Exception:
            pass
        # Model loop
        _nsfw_batch = False
        try:
            if channel is not None:
                _nsfw_batch = bool(getattr(channel, 'nsfw', False)) or bool(getattr(getattr(channel, 'parent', None), 'nsfw', False))
            # Apply participation.allow_nsfw gate
            from .config_service import ConfigService
            cfg = ConfigService('config.yaml')
            if not bool(cfg.participation().get('allow_nsfw', True)):
                _nsfw_batch = False
        except Exception:
            _nsfw_batch = False
        cfg_models = self.model_cfg.get('models')
        if isinstance(cfg_models, str):
            models_to_try = [m.strip() for m in cfg_models.split(',') if m.strip()]
        elif isinstance(cfg_models, list):
            models_to_try = [str(m) for m in cfg_models if str(m).strip()]
        else:
            models_to_try = []
        if model_override:
            prioritized: list[str] = []
            seen_models: set[str] = set()
            prioritized.append(model_override)
            seen_models.add(model_override)
            for m in models_to_try:
                if m and m not in seen_models:
                    prioritized.append(m)
                    seen_models.add(m)
            models_to_try = prioritized
        allow_auto = bool(self.model_cfg.get('allow_auto_fallback', False))
        stops = self.model_cfg.get('stop')
        correlation_id = f"{cid}-batch"
        reply = None
        # Recompute nsfw and build base context for provider selection
        try:
            if channel is not None:
                _nsfw_batch = bool(getattr(channel, 'nsfw', False)) or bool(getattr(getattr(channel, 'parent', None), 'nsfw', False))
        except Exception:
            _nsfw_batch = False
        base_cf = {'channel': cid, 'user': 'batch', 'correlation': correlation_id, 'nsfw': _nsfw_batch, 'has_images': False, 'web': True}
        # Map UI-provided provider override to index, if present
        override_index: int | None = None
        try:
            # events list contains the user event; check last one for override
            if events and isinstance(events[-1], dict):
                prov = str(events[-1].get('provider','')).strip().lower()
                if prov in ('openrouter','openai'):
                    base_cf['provider_name'] = prov
        except Exception:
            pass
        if model_override:
            inferred_provider = self._infer_provider_for_model(model_override, base_cf.get('provider_name'))
            if inferred_provider and inferred_provider != base_cf.get('provider_name'):
                base_cf['provider_name'] = inferred_provider
                try:
                    self.log.debug(
                        "[override-provider-hint] %s %s",
                        fmt('channel', cid),
                        fmt('provider', inferred_provider),
                    )
                except Exception:
                    pass
        # If web context selected a provider name, apply provider-based NSFW default when not already True/forced
        try:
            if base_cf.get('web') and not base_cf.get('nsfw'):
                prov = base_cf.get('provider_name')
                if prov == 'openai':
                    base_cf['nsfw'] = True
                elif prov == 'openrouter':
                    base_cf['nsfw'] = False
        except Exception:
            pass
        # Determine providers for this context if supported
        provider_indices = [0]
        try:
            if hasattr(self.llm, 'providers_for_context'):
                plist = self.llm.providers_for_context(base_cf)  # type: ignore[attr-defined]
                prov_name = base_cf.get('provider_name')
                if isinstance(plist, list) and plist:
                    indices = list(range(len(plist)))
                    if prov_name:
                        target_cls = 'OpenRouterClient' if prov_name == 'openrouter' else ('OpenAICompatClient' if prov_name == 'openai' else None)
                        if target_cls:
                            preferred = None
                            for i, p in enumerate(plist):
                                if p.__class__.__name__ == target_cls:
                                    preferred = i
                                    break
                            if preferred is not None:
                                provider_indices = [preferred] + [idx for idx in indices if idx != preferred]
                            else:
                                provider_indices = indices
                        else:
                            provider_indices = indices
                    elif len(indices) > 1:
                        provider_indices = indices
        except Exception:
            pass

        # Pre-fetch provider list for class inspection (to special-case openai-compat once)
        _providers_list = None
        try:
            if hasattr(self.llm, 'providers_for_context'):
                _providers_list = self.llm.providers_for_context(base_cf)  # type: ignore[attr-defined]
        except Exception:
            _providers_list = None

        for p_index in provider_indices:
            # Determine if current provider is OpenAI-compatible; if so, try only the first model once
            try:
                p_cls = None
                if isinstance(_providers_list, list) and 0 <= p_index < len(_providers_list):
                    p_cls = _providers_list[p_index].__class__.__name__
                is_openai_compat = (p_cls == 'OpenAICompatClient')
            except Exception:
                is_openai_compat = False

            _model_iter = models_to_try[:1] if is_openai_compat else models_to_try
            for idx, model_name in enumerate(_model_iter):
                if not model_name:
                    continue
                try:
                    start_ts = now_local()
                    # Router-level start log: mask model name when using OpenAICompat provider for clarity
                    disp_model = model_name
                    try:
                        if isinstance(_providers_list, list) and 0 <= p_index < len(_providers_list):
                            if _providers_list[p_index].__class__.__name__ == 'OpenAICompatClient':
                                disp_model = 'openai-compat'
                    except Exception:
                        pass
                    # Demote router-level start; provider selector logs authoritative start with provider
                    self.log.debug(f"[llm-start] {fmt('channel', cid)} {fmt('user','batch')} {fmt('model', disp_model)} {fmt('nsfw', base_cf.get('nsfw'))} {fmt('fallback_index', idx)} {fmt('correlation', correlation_id)}")
                    cf_send = dict(base_cf)
                    cf_send['provider_index'] = p_index
                    result = await self.llm.generate_chat(
                        messages,
                        max_tokens=self.model_cfg.get('max_tokens'),
                        model=model_name,
                        temperature=self.model_cfg.get('temperature'),
                        top_p=self.model_cfg.get('top_p'),
                        stop=stops,
                        context_fields=cf_send,
                    )
                    reply = result.get('text') if isinstance(result, dict) else result
                    provider_used = (result or {}).get('provider') if isinstance(result, dict) else None
                    # Optional prompt/response logging to files for batch/web mode
                    try:
                        from .config_service import ConfigService
                        cfg = ConfigService('config.yaml')
                        if bool(cfg.log_prompts()):
                            ts_dir = now_local().strftime('prompts-%Y%m%d-%H%M%S')
                            out_dir = Path('logs') / ts_dir
                            out_dir.mkdir(parents=True, exist_ok=True)
                            import json
                            p = {
                                'correlation': correlation_id,
                                'channel': cid,
                                'user': 'batch',
                                'model': model_name,
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
                    usage = (result or {}).get('usage') if isinstance(result, dict) else None
                    dur_ms = int((now_local() - start_ts).total_seconds() * 1000)
                    # Hide model when using openai-compatible backend
                    hide_model = (provider_used == 'openai')
                    if usage and (usage.get('input_tokens') is not None or usage.get('output_tokens') is not None):
                        if hide_model:
                            self.log.info(f"[llm-finish] {fmt('channel', cid)} {fmt('user','batch')} {fmt('provider', provider_used or 'unknown')} {fmt('duration_ms', dur_ms)} {fmt('nsfw', _nsfw_batch)} {fmt('tokens_in', usage.get('input_tokens','NA'))} {fmt('tokens_out', usage.get('output_tokens','NA'))} {fmt('total_tokens', usage.get('total_tokens','NA'))} {fmt('fallback_index', idx)} {fmt('correlation', correlation_id)}")
                        else:
                            self.log.info(f"[llm-finish] {fmt('channel', cid)} {fmt('user','batch')} {fmt('model', model_name)} {fmt('provider', provider_used or 'unknown')} {fmt('duration_ms', dur_ms)} {fmt('nsfw', _nsfw_batch)} {fmt('tokens_in', usage.get('input_tokens','NA'))} {fmt('tokens_out', usage.get('output_tokens','NA'))} {fmt('total_tokens', usage.get('total_tokens','NA'))} {fmt('fallback_index', idx)} {fmt('correlation', correlation_id)}")
                    else:
                        if hide_model:
                            self.log.info(f"[llm-finish] {fmt('channel', cid)} {fmt('user','batch')} {fmt('provider', provider_used or 'unknown')} {fmt('duration_ms', dur_ms)} {fmt('nsfw', _nsfw_batch)} {fmt('fallback_index', idx)} {fmt('correlation', correlation_id)}")
                        else:
                            self.log.info(f"[llm-finish] {fmt('channel', cid)} {fmt('user','batch')} {fmt('model', model_name)} {fmt('provider', provider_used or 'unknown')} {fmt('duration_ms', dur_ms)} {fmt('nsfw', _nsfw_batch)} {fmt('fallback_index', idx)} {fmt('correlation', correlation_id)}")
                    break
                except Exception as e:
                    self.log.error(f"LLM error with {model_name}: {e}")
                    reply = None
            if reply:
                break
        if reply is None and allow_auto:
            try:
                # Keep previously computed nsfw flag
                start_ts = now_local()
                result = await self.llm.generate_chat(
                    messages,
                    max_tokens=self.model_cfg.get('max_tokens'),
                    model='openrouter/auto',
                    temperature=self.model_cfg.get('temperature'),
                    top_p=self.model_cfg.get('top_p'),
                    stop=stops,
                    # For auto-fallback we keep web/nsfw flags; selector may still choose web list,
                    # but OpenRouter client will ultimately be targeted due to model label.
                    context_fields={'channel': cid, 'user': 'batch', 'correlation': correlation_id, 'nsfw': _nsfw_batch, 'has_images': False, 'web': True},
                )
                reply = result.get('text') if isinstance(result, dict) else result
                provider_used = (result or {}).get('provider') if isinstance(result, dict) else None
                dur_ms = int((now_local() - start_ts).total_seconds() * 1000)
                self.log.info(f"[llm-finish] {fmt('channel', cid)} {fmt('user','batch')} {fmt('model','openrouter/auto')} {fmt('provider', provider_used or 'openrouter')} {fmt('duration_ms', dur_ms)} {fmt('nsfw', _nsfw_batch)} {fmt('correlation', correlation_id)}")
            except Exception as e2:
                self.log.error(f"LLM auto fallback error: {e2}")
        return reply
