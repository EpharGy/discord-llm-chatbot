from __future__ import annotations

import json
from typing import Any


def quote_value(value: Any) -> str:
    if value is None:
        return "NA"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    # Strings & others: JSON-escape then strip surrounding quotes
    try:
        s = json.dumps(str(value))
        if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
            s = s[1:-1]
        return f'"{s}"'
    except Exception:
        return f'"{str(value)}"'


def fmt(key: str, value: Any) -> str:
    return f"{key}={quote_value(value)}"
