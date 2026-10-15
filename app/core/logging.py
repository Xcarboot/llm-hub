import json
import logging
import re
from collections.abc import Mapping
from typing import Any

SENSITIVE_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "x-admin-token",
    "messages",
    "message",
    "prompt",
    "content",
}


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if str(key).lower() in SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def redact_text(value: str) -> str:
    value = re.sub(r"(?i)Bearer\s+[^\s,;]+", "Bearer [REDACTED]", value)
    return re.sub(r"gw_[A-Za-z0-9_-]+", "gw_[REDACTED]", value)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        extra = getattr(record, "event_data", None)
        if extra is not None:
            data["data"] = redact(extra)
        return json.dumps(data, ensure_ascii=False, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
