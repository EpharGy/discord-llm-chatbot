from __future__ import annotations

from collections import defaultdict, deque
from typing import Deque, Dict, List


class ConversationBatcher:
    def __init__(self, max_buffer_per_channel: int = 200):
        self._buffers: Dict[str, Deque[dict]] = defaultdict(lambda: deque(maxlen=max_buffer_per_channel))

    def add(self, channel_id: str, event: dict) -> None:
        self._buffers[channel_id].append(event)

    def drain(self, channel_id: str, limit: int = 10) -> List[dict]:
        buf = self._buffers[channel_id]
        out: List[dict] = []
        while buf and len(out) < max(1, limit):
            out.append(buf.popleft())
        return out

    def channels(self) -> List[str]:
        return [cid for cid, buf in self._buffers.items() if len(buf) > 0]

    def clear(self, channel_id: str) -> None:
        self._buffers[channel_id].clear()
