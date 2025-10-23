from __future__ import annotations

from datetime import datetime
import re
import random
from .logger_factory import get_logger
from .utils.logfmt import fmt
from .utils.time_utils import ensure_local, now_local


class ParticipationPolicy:
    def __init__(self, rate_limits: dict, participation: dict):
        self.log = get_logger("ParticipationPolicy")
        # New configurable behavior
        self.mention_required = bool(participation.get("mention_required", True))
        self.respond_to_name = bool(participation.get("respond_to_name", True))
        self.aliases = set(a.lower() for a in participation.get("name_aliases", []))
        # Name matching mode: 'strict' (spaces on both sides) | 'loose' (alias at token start, may be followed by a valid separator)
        self.name_matching_mode = str(participation.get("name_matching", "strict")).strip().lower()
        if self.name_matching_mode not in ("strict", "loose"):
            self.name_matching_mode = "strict"
        # Precompile regex patterns per alias for fast checks (isolate errors per-alias; never drop all)
        self._alias_patterns: list[tuple[str, re.Pattern[str]]] = []
        for a in self.aliases:
            if not a:
                continue
            try:
                esc = re.escape(a)
                if self.name_matching_mode == "strict":
                    # Strict: alias must not be preceded by a word char or hyphen (so no 'don-mai' or 'main'),
                    # and must be followed by optional possessive/contraction (’s, 'd, etc.) then
                    # end/space/or natural punctuation. Include ) ] } , . ! ? : ; and right double-quote.
                    pat = re.compile(rf"(?<![\w-]){esc}(?=(?:['’](?:s|d|m|re|ll))?(?:$|\s|[\)\]}} ,\.\!\?\:\;”]))")
                else:
                    # loose: alias must be at start of string or preceded by whitespace, and
                    # must be followed by end/whitespace or a valid separator (e.g., '-', '_', ':', ',', '.', ';', '!', '?', ')', ']', '}').
                    # Keep all trailing allowed chars inside one character class so it compiles consistently.
                    pat = re.compile(rf"(?:(?<=\s)|^){esc}(?=$|[\s\-_:,\.\;\!\?\)\]}}])")
                self._alias_patterns.append((a, pat))
            except re.error as e:
                try:
                    self.log.warning(f"[alias-regex-error] alias={a} mode={self.name_matching_mode} err={e}")
                except Exception:
                    pass
                continue
        try:
            self.log.debug(
                f"[alias-config] mode={self.name_matching_mode} aliases={list(self.aliases)} patterns={len(self._alias_patterns)}"
            )
        except Exception:
            pass

        cooldown = participation.get("cooldown", {})
        self.cooldown_min_messages = int(cooldown.get(
            "min_messages_between_replies",
            rate_limits.get("min_messages_between_replies", 10),
        ))
        self.cooldown_min_seconds = int(cooldown.get(
            "min_seconds_between_replies",
            rate_limits.get("min_seconds_between_replies", 1800),
        ))
        logic_type = str(cooldown.get("logic_type", "OR")).strip().upper()
        self.cooldown_logic_type = logic_type if logic_type in {"AND", "OR"} else "OR"
        # Anti-spam from top-level rate_limits
        self.window_seconds = int(rate_limits.get("window_seconds", 300))
        self.max_responses = int(rate_limits.get("max_responses", 8))
        self.warning_ttl_seconds = int(rate_limits.get("warning_ttl_seconds", 5))

        self.random_response_chance = float(participation.get("random_response_chance", 0.2))

        ctx_time = participation.get("context_on_time_cooldown", {})
        self.time_ctx_minutes = int(ctx_time.get("minutes", 10))
        self.time_ctx_max_messages = int(ctx_time.get("max_messages", 50))

        general = participation.get("general_chat", {})
        raw_allowed = general.get("allowed_channels", [])
        # Normalize to set of strings
        if isinstance(raw_allowed, str):
            self.allowed_general_channels = set(x.strip() for x in raw_allowed.split(",") if x.strip())
        else:
            self.allowed_general_channels = set(str(x) for x in raw_allowed)

        # Per-channel override: treat random_response_chance as 1.0 (correct key only)
        raw_override = general.get("response_chance_override", "")
        if isinstance(raw_override, str):
            self.response_chance_override_channels = set(x.strip() for x in raw_override.split(",") if x.strip())
        else:
            self.response_chance_override_channels = set(str(x) for x in (raw_override or []))

        # Conversation mode settings
        self.conversation_mode = participation.get("conversation_mode", {
            "enabled": False,
            "window_seconds": 120,
            "max_messages": 5,
            "include_non_replies": False,
        })

        # Bot interaction settings
        bots = participation.get("bots", {})
        self.respond_to_bots = bool(bots.get("respond_to_bots", False))
        # New: blocked list (takes precedence)
        blocked_bot_ids = bots.get("blocked_bot_ids", "")
        if isinstance(blocked_bot_ids, str):
            self.blocked_bot_ids = set(x.strip() for x in blocked_bot_ids.split(",") if x.strip())
        else:
            self.blocked_bot_ids = set(str(x) for x in blocked_bot_ids)
        # Legacy: allowed list (if present, enforce as allow-only)
        allowed_bot_ids = bots.get("allowed_bot_ids", "")
        if isinstance(allowed_bot_ids, str):
            self.allowed_bot_ids = set(x.strip() for x in allowed_bot_ids.split(",") if x.strip())
        else:
            self.allowed_bot_ids = set(str(x) for x in allowed_bot_ids)

    def window_size(self) -> int:
        # Allow router to align with context window size if provided elsewhere
        return getattr(self, "_window_size", 10)

    def set_window_size(self, n: int) -> None:
        self._window_size = int(n)

    # New helper: channel with forced general chat override (100% chance & unlimited conv-mode budget)
    def is_response_chance_override(self, channel_id: str) -> bool:
        return channel_id in getattr(self, "response_chance_override_channels", set())

    def _log_decision(self, event: dict, allow: bool, reason: str, style: str | None = None) -> None:
        try:
            channel = event.get("channel_name") or event.get("channel_id")
            user = event.get("author_name") or event.get("author_id")
            msg = event.get("message_id", "")
            corr = event.get("correlation", "")
            line = (
                f"[Decision] "
                f"{fmt('allow', bool(allow))} "
                f"{fmt('channel', channel)} "
                f"{fmt('user', user)} "
                f"{fmt('reason', reason)} "
                f"{fmt('style', style or 'normal')} "
                f"{fmt('msg', msg)} "
                f"{fmt('correlation', corr)}"
            )
            if allow:
                self.log.info(line)
            else:
                self.log.debug(line)
        except Exception:
            # Never let logging break decision flow
            pass

    def should_reply(self, event: dict, memory) -> dict:
        now = now_local()

        # Bots: allow only if configured
        if event.get("is_bot"):
            if not self.respond_to_bots:
                self._log_decision(event, False, "ignore-bot")
                return {"allow": False, "reason": "ignore-bot"}
            aid = str(event.get("author_id"))
            # Blocked list wins
            if aid in getattr(self, "blocked_bot_ids", set()):
                self._log_decision(event, False, "bot-blocked")
                return {"allow": False, "reason": "bot-blocked"}
            # Legacy allow list, if present, restricts to that list
            if getattr(self, "allowed_bot_ids", set()):
                if aid not in self.allowed_bot_ids:
                    self._log_decision(event, False, "bot-not-allowed")
                    return {"allow": False, "reason": "bot-not-allowed"}

        # Recency is now handled via context_on_time_cooldown at render time rather than strict gating here

        # Anti-spam per channel: limit to N responses in the last window_seconds
        if memory.responses_in_window(event["channel_id"], self.window_seconds) >= self.max_responses:
            self._log_decision(event, False, "anti-spam")
            return {"allow": False, "reason": "anti-spam", "ephemeral": True}

        # Mentions/name triggers vs general chat
        content = (event.get("content") or "").lower()
        name_matched = self.name_match(content) if self.respond_to_name else False
        try:
            self.log.debug(
                f"[name-check] mode={self.name_matching_mode} matched={bool(name_matched)} content_preview={content[:60]}"
            )
        except Exception:
            pass
        is_direct = bool(event.get("is_mentioned", False) or name_matched or event.get("is_reply_to_bot", False))

        # If mention is required, only respond to direct mentions or name aliases
        if self.mention_required and not is_direct:
            self._log_decision(event, False, "mention-required")
            return {"allow": False, "reason": "mention-required"}

        last_reply = memory.last_reply_info(event["channel_id"]) or ensure_local(datetime.fromtimestamp(0))
        if last_reply is None:
            last_reply = now
        seconds_since_last = (now - last_reply).total_seconds()
        messages_since_last = memory.messages_since_last_reply(event["channel_id"])  # per-channel count

        # If mention or name trigger: allowed (subject to anti-spam and recency), reply style
        if is_direct:
            reason = "mention-alias" if (name_matched and not event.get("is_mentioned", False)) else "mention"
            self._log_decision(event, True, reason, style="reply")
            return {"allow": True, "reason": reason, "style": "reply"}

        # General chat: probabilistic trigger with cooldowns, only in allowed channels
        if event["channel_id"] not in self.allowed_general_channels:
            self._log_decision(event, False, "general-not-allowed-channel")
            return {"allow": False, "reason": "general-not-allowed-channel"}

        # General chat: probabilistic trigger with cooldowns
        messages_ok = messages_since_last >= self.cooldown_min_messages
        seconds_ok = seconds_since_last >= self.cooldown_min_seconds
        if self.cooldown_logic_type == "AND":
            cooldown_ok = messages_ok and seconds_ok
        else:
            cooldown_ok = messages_ok or seconds_ok
        # Allow override channels to bypass cooldown (still subject to anti-spam and allowlist)
        is_override = event["channel_id"] in getattr(self, "response_chance_override_channels", set())
        if not cooldown_ok and not is_override:
            self._log_decision(event, False, "cooldown")
            return {"allow": False, "reason": "cooldown"}

        # Random chance gate
        if is_override:
            roll = 0.0  # force pass
        else:
            roll = random.random()
            if roll >= self.random_response_chance:
                self._log_decision(event, False, f"chance-failed:{roll:.2f}")
                return {"allow": False, "reason": f"chance-failed:{roll:.2f}"}

        # If cooldown was by time, indicate to use time-bounded context
        context_hint = None
        if seconds_since_last >= self.cooldown_min_seconds:
            context_hint = {
                "time_bound_minutes": self.time_ctx_minutes,
                "max_messages": self.time_ctx_max_messages,
            }

        reason = "general-override" if is_override else "general"
        self._log_decision(event, True, reason, style="normal")
        return {"allow": True, "reason": reason, "style": "normal", "context_hint": context_hint}

    def name_match(self, content: str) -> bool:
        """Name alias match according to configured mode.

        strict: requires a real space on both sides of alias (no start/end matches).
        loose: alias at start of token (start of string or preceded by whitespace) and followed by
               end/whitespace or a valid separator (e.g., '-') so 'mai-chan' matches; 'main' and 'don-mai' do not.
        """
        if not self.respond_to_name or not content:
            return False
        try:
            s = content.lower()
        except Exception:
            s = str(content)
        for alias, p in self._alias_patterns:
            try:
                if p.search(s):
                    try:
                        self.log.debug(
                            f"[name-match] mode={self.name_matching_mode} alias={alias} content_preview={s[:50]}"
                        )
                    except Exception:
                        pass
                    return True
            except Exception:
                continue
        return False
