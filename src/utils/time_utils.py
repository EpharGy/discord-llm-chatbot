from __future__ import annotations

import os
from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%f%z"

_APP_TIMEZONE: str | None = None


def set_app_timezone(tz_name: str | None) -> None:
    """Set an optional application timezone override.

    Accepted values:
    - ``None`` or ``"system"``: use environment/system local timezone
    - ``"UTC"``
    - IANA timezone name (e.g. ``"Australia/Adelaide"``)
    """
    global _APP_TIMEZONE
    if not tz_name:
        _APP_TIMEZONE = None
        return
    value = str(tz_name).strip()
    if not value:
        _APP_TIMEZONE = None
        return
    if value.lower() in {"system", "local"}:
        _APP_TIMEZONE = None
        return
    _APP_TIMEZONE = value


def _tz_from_name(name: str | None) -> tzinfo | None:
    if not name:
        return None
    value = str(name).strip()
    if not value or value.lower() in {"system", "local"}:
        return None
    if value.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError:
        return None


def local_tzinfo() -> tzinfo:
    """Resolve the app's effective local timezone.

    Resolution order:
    1) explicit app override via ``set_app_timezone``
    2) ``BOT_TIMEZONE`` environment variable
    3) ``TZ`` environment variable
    4) system timezone from ``datetime.now().astimezone()``
    5) UTC fallback
    """
    if _APP_TIMEZONE is not None:
        tz = _tz_from_name(_APP_TIMEZONE)
        if tz is not None:
            return tz
        system_tz = datetime.now().astimezone().tzinfo
        if system_tz is not None:
            return system_tz
        return timezone.utc

    for candidate in (os.getenv("BOT_TIMEZONE"), os.getenv("TZ")):
        tz = _tz_from_name(candidate)
        if tz is not None:
            return tz
    system_tz = datetime.now().astimezone().tzinfo
    if system_tz is not None:
        return system_tz
    return timezone.utc


def now_local() -> datetime:
    """Return the current time as an aware datetime in the system's local timezone."""
    return datetime.now(local_tzinfo())


def ensure_local(dt: datetime | None) -> datetime | None:
    """Coerce a datetime into the system's local timezone.

    Naive datetimes are assumed to already represent local time and will be tagged
    with the system's timezone. Aware datetimes are converted to the local timezone.
    """
    if dt is None:
        return None
    tz = local_tzinfo()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def format_local(dt: datetime) -> str:
    """Format a datetime using the shared local ISO pattern."""
    local_dt = ensure_local(dt)
    assert local_dt is not None  # for type checkers; dt is never None here
    return local_dt.strftime(ISO_FORMAT)
