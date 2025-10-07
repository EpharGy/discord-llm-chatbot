from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Deque, Dict, List

from .utils.time_utils import now_local


@dataclass
class PendingMention:
    channel_id: str
    message_id: int
    style: str = "reply"
    enqueued_at: datetime = field(default_factory=now_local)


class MentionsQueue:
    def __init__(self, max_per_channel: int = 100):
        self._q: Dict[str, Deque[PendingMention]] = defaultdict(deque)
        self._max = max_per_channel

    def enqueue(self, item: PendingMention) -> bool:
        dq = self._q[item.channel_id]
        if len(dq) >= self._max:
            return False
        dq.append(item)
        return True

    def peek(self, channel_id: str) -> PendingMention | None:
        dq = self._q[channel_id]
        return dq[0] if dq else None

    def pop(self, channel_id: str) -> PendingMention | None:
        dq = self._q[channel_id]
        return dq.popleft() if dq else None

    def channels(self) -> List[str]:
        return [cid for cid, dq in self._q.items() if dq]

    def size(self, channel_id: str) -> int:
        return len(self._q[channel_id])
