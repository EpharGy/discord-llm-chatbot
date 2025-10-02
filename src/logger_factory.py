import logging
from logging.handlers import RotatingFileHandler
import os
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_CONFIGURED = False
_FULL_ENABLED = False


class _TzFormatter(logging.Formatter):
    def __init__(self, *args, tz: Optional[str] = None, **kwargs):
        super().__init__(*args, **kwargs)
        # Timezone selection:
        # - tz == "UTC": force UTC
        # - tz is None or "system": use local system timezone
        # - else: try specified IANA zone, fall back to system if unavailable
        import datetime as _dt
        if tz == "UTC":
            self._tz = _dt.timezone.utc
        elif tz is None or tz == "system":
            self._tz = _dt.datetime.now().astimezone().tzinfo
        else:
            try:
                self._tz = ZoneInfo(tz)
            except ZoneInfoNotFoundError:
                self._tz = _dt.datetime.now().astimezone().tzinfo

    def formatTime(self, record, datefmt=None):
        # record.created is epoch seconds (float)
        import datetime as _dt
        dt = _dt.datetime.fromtimestamp(record.created, tz=self._tz or _dt.datetime.now().astimezone().tzinfo)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat()


def configure_logging(level: Optional[str] = None, tz: Optional[str] = None, fmt: str = "text", lib_log_level: Optional[str] = None, console_to_file: bool | None = None, error_file: bool | None = None) -> None:
    global _CONFIGURED, _FULL_ENABLED
    if _CONFIGURED:
        return
    # Map levels
    lvl = (level or "INFO").upper()
    if lvl not in ("INFO", "DEBUG", "FULL"):
        lvl = "INFO"
    py_level = logging.DEBUG if lvl in ("DEBUG", "FULL") else logging.INFO
    _FULL_ENABLED = (lvl == "FULL")

    root = logging.getLogger()
    root.setLevel(py_level)
    # Remove existing handlers to avoid duplicate logs
    for h in list(root.handlers):
        root.removeHandler(h)

    if fmt == "json":
        # Simple JSON-like output (without extra deps); keep it minimal
        pattern = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
    else:
        pattern = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"

    # Always log to console
    handler = logging.StreamHandler()
    handler.setLevel(py_level)
    handler.setFormatter(_TzFormatter(pattern, tz=tz, datefmt="%Y-%m-%d %H:%M:%S%z"))
    root.addHandler(handler)

    # Optional: mirror console logs to logs/log.log when LOG_CONSOLE=true
    mirror_enabled = console_to_file if console_to_file is not None else None
    env_console = os.getenv("LOG_CONSOLE")
    if env_console is not None:
        mirror_enabled = str(env_console).lower() in ("1","true","yes","on")
    if mirror_enabled:
        try:
            os.makedirs("logs", exist_ok=True)
            general_file = RotatingFileHandler(
                filename="logs/log.log",
                mode="a",
                maxBytes=1_000_000,
                backupCount=5,
                encoding="utf-8",
                delay=False,
            )
            general_file.setLevel(py_level)
            general_file.setFormatter(_TzFormatter(pattern, tz=tz, datefmt="%Y-%m-%d %H:%M:%S%z"))
            root.addHandler(general_file)
        except Exception:
            pass

    # ERROR file logging: always write ERROR+ to logs/errors.log when LOG_ERRORS=true
    errors_enabled = str(os.getenv("LOG_ERRORS", "")).lower() in ("1","true","yes","on")
    if error_file is not None:
        errors_enabled = bool(error_file)
    if errors_enabled:
        try:
            os.makedirs("logs", exist_ok=True)
            # Rotate at ~1MB with up to 5 backups
            err_handler = RotatingFileHandler(
                filename="logs/errors.log",
                mode="a",
                maxBytes=1_000_000,
                backupCount=5,
                encoding="utf-8",
                delay=False,
            )
            err_handler.setLevel(logging.ERROR)
            err_handler.setFormatter(_TzFormatter(pattern, tz=tz, datefmt="%Y-%m-%d %H:%M:%S%z"))
            root.addHandler(err_handler)
        except Exception:
            # Don't break startup due to file I/O
            pass

    # Quiet noisy third-party libraries by default (still show WARN/ERROR)
    lib_level_name = lib_log_level or os.getenv("LIB_LOG_LEVEL") or os.getenv("LIV_LOG_LEVEL")
    if lib_level_name:
        lib_level = getattr(logging, lib_level_name.upper(), logging.WARNING)
    else:
        lib_level = logging.WARNING
    for name in (
        "discord",
        "discord.http",
        "discord.gateway",
        "discord.client",
        "httpx",
        "httpcore",
    ):
        logging.getLogger(name).setLevel(lib_level)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    # If not configured explicitly, default to INFO, Adelaide time, text format
    if not _CONFIGURED:
        configure_logging(level="INFO", tz="Australia/Adelaide", fmt="text")
    return logging.getLogger(name)


def is_full_enabled() -> bool:
    return _FULL_ENABLED


def set_log_levels(level: Optional[str] = None, lib_log_level: Optional[str] = None) -> None:
    """Dynamically adjust root and library logger levels without reinitializing handlers.

    - level: "INFO" | "DEBUG" | "FULL" (FULL behaves like DEBUG but flips the internal flag)
    - lib_log_level: applies to known noisy libraries (discord/httpx/httpcore)
    """
    global _FULL_ENABLED
    lvl = (level or "INFO").upper()
    if lvl not in ("INFO", "DEBUG", "FULL"):
        lvl = "INFO"
    py_level = logging.DEBUG if lvl in ("DEBUG", "FULL") else logging.INFO
    _FULL_ENABLED = (lvl == "FULL")

    root = logging.getLogger()
    root.setLevel(py_level)
    for h in root.handlers:
        try:
            h.setLevel(py_level)
        except Exception:
            pass

    if lib_log_level:
        lib_level = getattr(logging, lib_log_level.upper(), logging.WARNING)
    else:
        lib_level = None
    if lib_level is not None:
        for name in ("discord", "discord.http", "discord.gateway", "discord.client", "httpx", "httpcore"):
            try:
                logging.getLogger(name).setLevel(lib_level)
            except Exception:
                pass
