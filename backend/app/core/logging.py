"""Structured logging with automatic secret/credential redaction."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

_SECRET_PATTERNS = [
    (re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(sk-[A-Za-z0-9_\-]{12,})"), "[REDACTED_KEY]"),
    (re.compile(r"(tvly-[A-Za-z0-9_\-]{8,})"), "[REDACTED_TAVILY]"),
    (re.compile(r"(AIza[A-Za-z0-9_\-]{16,})"), "[REDACTED_GOOGLE_KEY]"),
    (re.compile(r'("?(?:password|secret|token|api_key|refresh_token|access_token|cookie)"?\s*[:=]\s*"?)([^",\s}]+)', re.IGNORECASE), r"\1[REDACTED]"),
]


def redact_text(value: str) -> str:
    result = value
    for pattern, repl in _SECRET_PATTERNS:
        result = pattern.sub(repl, result)
    return result


class RedactingJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        if hasattr(record, "extra_fields") and isinstance(record.extra_fields, dict):
            for k, v in record.extra_fields.items():
                payload[k] = redact_text(str(v)) if isinstance(v, str) else v
        if record.exc_info:
            payload["exc"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingJsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
