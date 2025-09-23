from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone


class ConversationMemory:
    replies_timestamps: dict[str, deque]
    def __init__(self):
        self.store: dict[str, deque] = defaultdict(lambda: deque(maxlen=200))
        self.last_reply: dict[str, datetime] = {}
        self.replies_timestamps = defaultdict(lambda: deque())
        # conversation mode tracking per channel
        self.conv_until: dict[str, datetime] = {}
        self.conv_budget: dict[str, int] = {}
        # track message ids we have already responded to (per channel)
        self.responded_to: dict[str, set[str]] = defaultdict(set)

    def record(self, event: dict):
        cid = event["channel_id"]
        self.store[cid].append(event)

    def get_recent(self, channel_id: str, limit: int = 10):
        return list(self.store[channel_id])[-limit:]

    def get_recent_since(self, channel_id: str, cutoff_dt: datetime):
        return [e for e in self.store[channel_id] if e.get("created_at") and e["created_at"] >= cutoff_dt]

    def last_reply_info(self, channel_id: str):
        return self.last_reply.get(channel_id)

    def on_replied(self, event: dict):
        now = datetime.now(timezone.utc)
        cid = event["channel_id"]
        self.last_reply[cid] = now
        q = self.replies_timestamps[cid]
        q.append(now)
        # Keep only last minute window timestamps
        one_min_ago = now - timedelta(seconds=60)
        while q and q[0] < one_min_ago:
            q.popleft()
        # remember the triggering message id as handled
        msg_id = event.get("message_id")
        if msg_id:
            self.responded_to[cid].add(str(msg_id))

    def has_responded_to(self, channel_id: str, message_id: str | None) -> bool:
        if not message_id:
            return False
        return str(message_id) in self.responded_to.get(channel_id, set())

    def record_response_only(self, channel_id: str):
        """Record a reply timestamp for anti-spam without updating last_reply.

        Use this when conversation-mode replies should not affect cooldown but
        must still count towards the anti-spam window.
        """
        now = datetime.now(timezone.utc)
        q = self.replies_timestamps[channel_id]
        q.append(now)
        one_min_ago = now - timedelta(seconds=60)
        while q and q[0] < one_min_ago:
            q.popleft()

    def start_conversation_mode(self, channel_id: str, window_seconds: int, max_messages: int):
        now = datetime.now(timezone.utc)
        self.conv_until[channel_id] = now + timedelta(seconds=window_seconds)
        self.conv_budget[channel_id] = int(max_messages)

    def conversation_mode_active(self, channel_id: str) -> bool:
        until = self.conv_until.get(channel_id)
        if not until:
            return False
        now = datetime.now(timezone.utc)
        if now <= until and self.conv_budget.get(channel_id, 0) > 0:
            return True
        # Expire if past time or budget depleted
        self.conv_until.pop(channel_id, None)
        self.conv_budget.pop(channel_id, None)
        return False

    def consume_conversation_message(self, channel_id: str) -> bool:
        if not self.conversation_mode_active(channel_id):
            return False
        self.conv_budget[channel_id] = max(0, self.conv_budget.get(channel_id, 0) - 1)
        return True

    def responses_in_window(self, channel_id: str, window_seconds: int) -> int:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=window_seconds)
        q = self.replies_timestamps[channel_id]
        while q and q[0] < cutoff:
            q.popleft()
        return len(q)

    def messages_since_last_reply(self, channel_id: str) -> int:
        last = self.last_reply.get(channel_id)
        # Count only user (non-bot) messages
        if not last:
            return sum(1 for e in self.store[channel_id] if not e.get("is_bot"))
        # count user messages with created_at after last reply
        cnt = 0
        for e in reversed(self.store[channel_id]):
            if e.get("created_at") and e["created_at"] > last:
                if not e.get("is_bot"):
                    cnt += 1
            else:
                break
        return cnt
