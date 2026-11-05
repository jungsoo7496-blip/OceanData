"""Idempotency-Key 처리 (domain.md 7장, api-conventions.md)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import IdempotencyRecord
from app.domain.principal import Principal
from app.errors import ProblemError
from app.ports.clock import ClockPort


def body_hash(body: Any) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _lock_id(pk: tuple[str, str, str, str]) -> int:
    """키 범위 (기관, 주체, 메서드+경로, 키)를 PostgreSQL 권고 잠금 번호(부호 있는 64비트)로."""
    digest = hashlib.sha256("\n".join(pk).encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _stored(db: Session, pk: tuple[str, str, str, str]) -> IdempotencyRecord | None:
    # 다른 트랜잭션이 방금 확정한 기록을 봐야 하므로 세션에 남은 객체가 아니라 DB에서 새로 읽는다
    return db.get(IdempotencyRecord, pk, populate_existing=True)


def _replay(rec: IdempotencyRecord, h: str) -> tuple[int, dict[str, Any], bool]:
    if rec.body_hash != h:
        raise ProblemError("IDEMPOTENCY_CONFLICT")
    return rec.status_code, rec.response_body or {}, True


def run_idempotent(db: Session, clock: ClockPort, retention_s: int, principal: Principal, method_path: str,
                   key: str | None, body: Any, fn: Callable[[], tuple[int, dict[str, Any]]]
                   ) -> tuple[int, dict[str, Any], bool]:
    """(status, body, replayed)를 돌려준다. 같은 키·같은 본문이면 처음 응답, 다른 본문이면 409.

    같은 키로 동시에 들어온 요청은 트랜잭션 권고 잠금으로 줄을 세운다 (검토 2). 먼저 잡은 요청이 커밋(응답 전,
    deps.DB)하거나 되돌릴 때까지 다음 요청은 기다렸다가, 확정된 기록을 다시 주거나 409를 준다. 잠금을 거치지 않은
    쓰기와 부딪혀 기록 저장이 고유 제약에 걸리면, 이 요청의 작업을 되돌리고 먼저 확정된 기록을 기준으로 답한다.
    """
    if not key:
        status, resp = fn()
        return status, resp, False
    if len(key) > 200:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/header/idempotency-key", "code": "too_long",
                                                        "message": "Idempotency-Key는 200자 이하입니다"}])
    now = clock.now()
    h = body_hash(body)
    pk = (principal.organization_id, principal.principal_id, method_path, key)
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _lock_id(pk)})  # 트랜잭션이 끝나면 풀린다
    rec = _stored(db, pk)
    if rec is not None and rec.expires_at > now:
        return _replay(rec, h)
    if rec is not None:
        db.delete(rec)
        db.flush()
    status, resp = fn()
    db.flush()  # 작업 자체의 제약 오류는 그대로 올려 보낸다 (아래에서 기록 충돌과 섞이지 않게)
    db.add(IdempotencyRecord(organization_id=pk[0], principal_id=pk[1], method_path=pk[2], key=pk[3],
                             body_hash=h, status_code=status, response_body=resp, created_at=now,
                             expires_at=now + timedelta(seconds=retention_s)))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()  # 이 요청이 만든 것은 모두 되돌린다 (먼저 확정된 요청이 결과다)
        rec = _stored(db, pk)
        if rec is None:
            raise
        return _replay(rec, h)
    return status, resp, False
