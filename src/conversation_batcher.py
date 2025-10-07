from __future__ import annotations

from collections import defaultdict, deque
from typing import Deque, Dict, List

from .logger_factory import get_logger
from .utils.logfmt import fmt


class ConversationBatcher:
    def __init__(self, max_buffer_per_channel: int = 200):
        self._max_buffer = max_buffer_per_channel
        self._buffers: Dict[str, Deque[dict]] = defaultdict(deque)
        self._seen_sets: Dict[str, set[str]] = defaultdict(set)
        self._seen_order: Dict[str, Deque[str]] = defaultdict(deque)
        self._log = get_logger("ConversationBatcher")

    def _cleanup_seen(self, channel_id: str) -> None:
        seen_order = self._seen_order.get(channel_id)
        if seen_order is None:
            return
        seen_set = self._seen_sets.get(channel_id, set())
        while seen_order and seen_order[0] not in seen_set:
            seen_order.popleft()
        if not seen_order and not self._buffers.get(channel_id):
            # Drop empty structures to keep dicts small
            self._seen_order.pop(channel_id, None)
            self._seen_sets.pop(channel_id, None)
            self._buffers.pop(channel_id, None)

    def add(self, channel_id: str, event: dict) -> bool:
        msg_id_obj = event.get("message_id")
        msg_id = str(msg_id_obj) if msg_id_obj is not None else None
        if msg_id and msg_id in self._seen_sets[channel_id]:
            self._log.debug(
                f"batch-dedupe {fmt('channel', channel_id)} {fmt('msg', msg_id)}"
            )
            return False

        buf = self._buffers[channel_id]
        buf.append(event)
        if msg_id:
            self._seen_sets[channel_id].add(msg_id)
            self._seen_order[channel_id].append(msg_id)

        # Enforce max buffer size; drop oldest if necessary
        if len(buf) > self._max_buffer:
            old_event = buf.popleft()
            old_msg_obj = old_event.get("message_id")
            old_msg_id = str(old_msg_obj) if old_msg_obj is not None else None
            if old_msg_id and old_msg_id in self._seen_sets[channel_id]:
                self._seen_sets[channel_id].remove(old_msg_id)
        self._cleanup_seen(channel_id)
        return True

    def drain(self, channel_id: str, limit: int = 10) -> List[dict]:
        buf = self._buffers.get(channel_id)
        if not buf:
            return []
        out: List[dict] = []
        while buf and len(out) < max(1, limit):
            ev = buf.popleft()
            out.append(ev)
            msg_obj = ev.get("message_id")
            msg_id = str(msg_obj) if msg_obj is not None else None
            if msg_id and msg_id in self._seen_sets[channel_id]:
                self._seen_sets[channel_id].remove(msg_id)
        self._cleanup_seen(channel_id)
        return out

    def channels(self) -> List[str]:
        return [cid for cid, buf in self._buffers.items() if buf]

    def clear(self, channel_id: str) -> None:
        if channel_id in self._buffers:
            self._buffers[channel_id].clear()
        if channel_id in self._seen_sets:
            self._seen_sets[channel_id].clear()
        if channel_id in self._seen_order:
            self._seen_order[channel_id].clear()
        self._buffers.pop(channel_id, None)
        self._seen_sets.pop(channel_id, None)
        self._seen_order.pop(channel_id, None)
