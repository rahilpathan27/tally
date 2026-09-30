"""Structured JSON logging with mandatory redaction of card data and secrets.

Every service configures logging through ``configure_logging``. The filter runs on each record:
it masks card-number-like digit runs that pass the Luhn check (keeping the last four), drops
CVV-like fields, and replaces values of sensitive keys (secret, password, token, authorization,
signature, pan, cvv, otp, code) in structured ``extra`` data.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

_PAN = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_SENSITIVE_KEYS = re.compile(
    r"(secret|password|passwd|token|authorization|signature|pan|cvv|cvc|otp|code|cookie|"
    r"private_key|api_key)",
    re.IGNORECASE,
)
_KV = re.compile(
    r"(?i)\b(secret|password|token|authorization|signature|cvv|cvc|otp|api_key)"
    r"(\s*[=:]\s*)([^\s,;&\"']+)"
)
CONTEXT_FIELDS = ("merchant_id", "payment_id", "entry_id", "trace_id", "request_id", "actor")


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def redact_text(text: str) -> str:
    def mask(match: re.Match[str]) -> str:
        digits = re.sub(r"[ -]", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return f"[PAN ****{digits[-4:]}]"
        return match.group(0)

    return _KV.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", _PAN.sub(mask, text))


def redact(value: Any, key: str = "") -> Any:
    if key and _SENSITIVE_KEYS.search(key) and key not in CONTEXT_FIELDS:
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    return value


_RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message"}


class RedactingJsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = redact(value, key)
        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(service: str, level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingJsonFormatter(service))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
