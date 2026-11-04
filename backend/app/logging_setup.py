"""JSON 구조 로그 (01번 요구사항 4). 비밀값·사전 서명 URL·쿼리 문자열을 남기지 않는다."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

from app.context import organization_id_var, request_id_var

_SENSITIVE_KEY = re.compile(
    r"(pass(word)?|secret|token|api[_-]?key|authorization|cookie|signature|credential|client_secret|"
    r"enrollment_code|presigned|url_with_query)",
    re.IGNORECASE,
)
_URL = re.compile(r"(https?://[^\s?#\"']+)(\?[^\s#\"']*)?(#[^\s\"']*)?")
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]+=*")
_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime", "taskName"}
REDACTED = "[REDACTED]"


def strip_query(text: str) -> str:
    """문자열 안의 URL에서 쿼리 문자열·조각을 지운다 (01번 요구사항 4.4)."""
    return _URL.sub(lambda m: m.group(1), text)


def scrub(value: Any, key: str | None = None) -> Any:
    if key is not None and _SENSITIVE_KEY.search(key):
        return REDACTED
    if isinstance(value, str):
        return _BEARER.sub("Bearer " + REDACTED, strip_query(value))
    if isinstance(value, dict):
        return {k: scrub(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [scrub(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": request_id_var.get(),
            "organization_id": organization_id_var.get(),
            "event": scrub(record.getMessage()),
        }
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                payload[k] = scrub(v, k)
        if record.exc_info:
            payload["exc"] = scrub(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).handlers[:] = []
        logging.getLogger(noisy).propagate = False
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
