"""Idempotency-Key 처리 (domain.md 7장, api-conventions.md)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import IdempotencyRecord
from app.domain.principal import Principal
from app.errors import ProblemError
from app.ports.clock import ClockPort


def body_hash(body: Any) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def run_idempotent(db: Session, clock: ClockPort, retention_s: int, principal: Principal, method_path: str,
                   key: str | None, body: Any, fn: Callable[[], tuple[int, dict[str, Any]]]
                   ) -> tuple[int, dict[str, Any], bool]:
    """(status, body, replayed)를 돌려준다. 같은 키·같은 본문이면 처음 응답, 다른 본문이면 409."""
    if not key:
        status, resp = fn()
        return status, resp, False
    if len(key) > 200:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/header/idempotency-key", "code": "too_long",
                                                        "message": "Idempotency-Key는 200자 이하입니다"}])
    now = clock.now()
    h = body_hash(body)
    pk = (principal.organization_id, principal.principal_id, method_path, key)
    rec = db.get(IdempotencyRecord, pk)
    if rec is not None and rec.expires_at > now:
        if rec.body_hash != h:
            raise ProblemError("IDEMPOTENCY_CONFLICT")
        return rec.status_code, rec.response_body or {}, True
    if rec is not None:
        db.delete(rec)
        db.flush()
    status, resp = fn()
    db.add(IdempotencyRecord(organization_id=pk[0], principal_id=pk[1], method_path=pk[2], key=pk[3],
                             body_hash=h, status_code=status, response_body=resp, created_at=now,
                             expires_at=now + timedelta(seconds=retention_s)))
    return status, resp, False
