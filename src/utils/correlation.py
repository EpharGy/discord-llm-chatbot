from __future__ import annotations


def make_correlation_id(channel_id: str | int, message_id: str | int) -> str:
    """Return a stable correlation id tying decision → llm → send.

    Current format: "<channelId>-<messageId>". Keep simple for grepability.
    """
    return f"{channel_id}-{message_id}"
