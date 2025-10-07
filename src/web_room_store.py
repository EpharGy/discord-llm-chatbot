from __future__ import annotations

import json
import hashlib
import os
import secrets
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Dict, List, Optional, Iterable

from .logger_factory import get_logger


ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime(ISO_FORMAT)


def _normalize_room_id(name: str) -> str:
    slug = name.strip().lower().replace(" ", "-") or "room"
    allowed = [c for c in slug if c.isalnum() or c in ("-", "_")]
    slug = "".join(allowed) or "room"
    return slug[:48]


@dataclass
class RoomMeta:
    room_id: str
    name: str
    created_at: str
    last_active: str
    passcode_salt: Optional[str] = None
    passcode_hash: Optional[str] = None
    provider: Optional[str] = None

    @property
    def requires_passcode(self) -> bool:
        return bool(self.passcode_hash)

    def to_dict(self) -> dict:
        data = asdict(self)
        if not data.get("provider"):
            data.pop("provider", None)
        return data

    @classmethod
    def from_dict(cls, entry: dict) -> "RoomMeta":
        return cls(
            room_id=entry["room_id"],
            name=entry.get("name", entry["room_id"]),
            created_at=entry.get("created_at", _now_iso()),
            last_active=entry.get("last_active", _now_iso()),
            passcode_salt=entry.get("passcode_salt"),
            passcode_hash=entry.get("passcode_hash"),
            provider=entry.get("provider"),
        )


class WebRoomStore:
    """Lightweight room store for web chat conversations."""

    def __init__(self, base_path: Path, default_room_id: str = "web-room", default_room_name: str = "General"):
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.base_path / "rooms.json"
        self._lock = RLock()
        self.log = get_logger("WebRoomStore")
        self._rooms: Dict[str, RoomMeta] = {}
        self._load_index()
        # Remove legacy rooms without passcodes
        to_remove = [rid for rid, meta in self._rooms.items() if not meta.requires_passcode]
        for rid in to_remove:
            self.log.info(f"room-store-remove-legacy room={rid}")
            self._delete_room_files(rid)
            self._rooms.pop(rid, None)
        if to_remove:
            self._save_index()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _load_index(self) -> None:
        if not self.meta_path.exists():
            return
        try:
            with self.meta_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            rooms = data.get("rooms", [])
            for entry in rooms:
                meta = RoomMeta.from_dict(entry)
                self._rooms[meta.room_id] = meta
        except Exception as e:
            self.log.error(f"room-store-load-error {e}")
            self._rooms = {}

    def _save_index(self) -> None:
        try:
            with self.meta_path.open("w", encoding="utf-8") as f:
                json.dump({"rooms": [r.to_dict() for r in self._rooms.values()]}, f, indent=2)
        except Exception as e:
            self.log.error(f"room-store-save-error {e}")

    def _room_dir(self, room_id: str) -> Path:
        d = self.base_path / room_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _messages_path(self, room_id: str) -> Path:
        return self._room_dir(room_id) / "messages.jsonl"

    def _hash_passcode(self, passcode: str, salt: str) -> str:
        return hashlib.sha256(f"{salt}:{passcode}".encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def list_rooms(self, as_dict: bool = False) -> List:
        with self._lock:
            rooms: Iterable[RoomMeta] = self._rooms.values()
            if as_dict:
                return [r.to_dict() for r in rooms]
            return list(rooms)

    def get_room(self, room_id: str) -> Optional[RoomMeta]:
        with self._lock:
            return self._rooms.get(room_id)

    def ensure_room(self, room_id: str, *, display_name: Optional[str] = None) -> RoomMeta:
        with self._lock:
            meta = self._rooms.get(room_id)
            if meta is None:
                name = display_name or room_id
                now = _now_iso()
                meta = RoomMeta(room_id=room_id, name=name, created_at=now, last_active=now)
                self._rooms[room_id] = meta
                self._save_index()
            return meta

    def create_room(self, name: str, passcode: Optional[str] = None) -> RoomMeta:
        if not passcode:
            raise ValueError("Passcode required")
        slug = _normalize_room_id(name)
        salt = secrets.token_hex(12) if passcode else None
        with self._lock:
            room_id = slug
            suffix = 1
            while room_id in self._rooms:
                suffix += 1
                room_id = f"{slug}-{suffix}"
            now = _now_iso()
            meta = RoomMeta(
                room_id=room_id,
                name=name.strip() or room_id,
                created_at=now,
                last_active=now,
                passcode_salt=salt,
                passcode_hash=self._hash_passcode(passcode, salt) if passcode and salt else None,
            )
            self._rooms[room_id] = meta
            self._save_index()
            return meta

    def validate_passcode(self, room_id: str, passcode: Optional[str]) -> bool:
        with self._lock:
            meta = self._rooms.get(room_id)
            if meta is None:
                return False
            if not meta.requires_passcode:
                return True
            if not passcode:
                return False
            try:
                expected = self._hash_passcode(passcode, meta.passcode_salt or "")
            except Exception:
                return False
            return secrets.compare_digest(expected, meta.passcode_hash or "")

    def update_last_active(self, room_id: str) -> None:
        with self._lock:
            meta = self._rooms.get(room_id)
            if meta is not None:
                meta.last_active = _now_iso()
                self._save_index()

    def set_provider(self, room_id: str, provider: Optional[str]) -> None:
        with self._lock:
            meta = self._rooms.get(room_id)
            if meta is not None:
                normalized = provider.lower() if isinstance(provider, str) else None
                if normalized not in {"openrouter", "openai"}:
                    normalized = None
                meta.provider = normalized
                self._save_index()

    def get_provider(self, room_id: str) -> Optional[str]:
        with self._lock:
            meta = self._rooms.get(room_id)
            return meta.provider if meta else None

    def append_message(self, room_id: str, message: dict) -> None:
        serialized = json.dumps(message, ensure_ascii=False)
        path = self._messages_path(room_id)
        try:
            with self._lock:
                with path.open("a", encoding="utf-8") as f:
                    f.write(serialized + "\n")
                self.update_last_active(room_id)
        except Exception as e:
            self.log.error(f"room-store-append-error room={room_id} err={e}")

    def load_messages(self, room_id: str, limit: int = 200) -> List[dict]:
        path = self._messages_path(room_id)
        if not path.exists():
            return []
        try:
            with self._lock:
                with path.open("r", encoding="utf-8") as f:
                    lines = f.readlines()
            selected = lines[-limit:]
            out: List[dict] = []
            for line in selected:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return out
        except Exception as e:
            self.log.error(f"room-store-load-messages room={room_id} err={e}")
            return []

    def clear_room(self, room_id: str) -> None:
        path = self._messages_path(room_id)
        with self._lock:
            try:
                if path.exists():
                    path.unlink()
                meta = self._rooms.get(room_id)
                if meta:
                    meta.last_active = _now_iso()
                    self._save_index()
            except Exception as e:
                self.log.error(f"room-store-clear-error room={room_id} err={e}")

    def delete_room(self, room_id: str) -> None:
        with self._lock:
            self._rooms.pop(room_id, None)
            self._save_index()
            self._delete_room_files(room_id)

    def _delete_room_files(self, room_id: str) -> None:
        room_dir = self.base_path / room_id
        try:
            if room_dir.exists():
                for root, _, files in os.walk(room_dir, topdown=False):
                    for file in files:
                        Path(root, file).unlink(missing_ok=True)
                    Path(root).rmdir()
        except Exception as e:
            self.log.error(f"room-store-delete-error room={room_id} err={e}")