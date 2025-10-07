from __future__ import annotations

from datetime import datetime

ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%f%z"


def now_local() -> datetime:
    """Return the current time as an aware datetime in the system's local timezone."""
    return datetime.now().astimezone()


def ensure_local(dt: datetime | None) -> datetime | None:
    """Coerce a datetime into the system's local timezone.

    Naive datetimes are assumed to already represent local time and will be tagged
    with the system's timezone. Aware datetimes are converted to the local timezone.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        tz = now_local().tzinfo
        return dt.replace(tzinfo=tz)
    return dt.astimezone()


def format_local(dt: datetime) -> str:
    """Format a datetime using the shared local ISO pattern."""
    local_dt = ensure_local(dt)
    assert local_dt is not None  # for type checkers; dt is never None here
    return local_dt.strftime(ISO_FORMAT)
