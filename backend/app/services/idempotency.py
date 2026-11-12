"""Idempotency-Key 처리 (domain.md 7장, api-conventions.md)."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db.models import IdempotencyRecord
from app.domain.principal import Principal
from app.errors import ProblemError, pg_code
from app.ports.clock import ClockPort

LOCK_WAIT_MS = 2_000  # 같은 키의 앞선 요청이 확정되기를 기다리는 최대 시간(ms). 넘으면 409 + Retry-After
# 같은 키 잠금을 동시에 기다릴 수 있는 요청 수(프로세스마다). 기다리는 요청은 스레드 하나와 DB 연결 하나를 쥐므로,
# 재시도 폭주가 스레드 풀·연결 풀을 다 쓰지 않게 넘치는 요청은 기다리지 않고 바로 409로 돌려보낸다 (ADR-0053)
MAX_LOCK_WAITERS = 8
_waiters = threading.BoundedSemaphore(MAX_LOCK_WAITERS)


def lock_scope(organization_id: str, principal_id: str, method_path: str, key: str) -> str:
    """같은 (기관, 주체, 메서드+경로, 키) 요청이 같은 DB 잠금을 쓰게 하는 문자열."""
    return "|".join(("idem", organization_id, principal_id, method_path, key))


def body_hash(body: Any) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _busy() -> ProblemError:
    return ProblemError("CONFLICT", headers={"Retry-After": "1"},
                        detail="같은 Idempotency-Key의 앞선 요청이 아직 처리 중입니다. 잠시 뒤 다시 보내세요")


def _serialize(db: Session, pk: tuple[str, str, str, str]) -> None:
    """같은 키의 동시 요청을 줄 세운다. 잠금은 트랜잭션이 끝날 때(커밋·롤백) 풀린다.

    요청 트랜잭션은 응답을 보내기 전에 커밋하므로(get_db의 function 범위, ADR-0053) 뒤 요청은 앞 요청의 확정된 기록을
    보고 같은 응답을 다시 받는다. 앞선 요청이 없으면 바로 진행한다(문장 하나). 있으면 최대 MAX_LOCK_WAITERS개 요청이
    LOCK_WAIT_MS까지만 기다리고, 넘치거나 시간이 지나면 기다리지 않고 409 + Retry-After로 곧 다시 보내게 한다."""
    scope = {"scope": lock_scope(*pk)}
    if db.scalar(text("SELECT pg_try_advisory_xact_lock(hashtextextended(:scope, 0))"), scope):
        return
    if not _waiters.acquire(blocking=False):
        raise _busy()
    try:
        db.execute(text(f"SET LOCAL lock_timeout = {int(LOCK_WAIT_MS)}"))
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"), scope)
        db.execute(text("SET LOCAL lock_timeout TO DEFAULT"))
    except DBAPIError as exc:
        if pg_code(exc) == "55P03":  # lock_not_available
            raise _busy() from None
        raise
    finally:
        _waiters.release()


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
    _serialize(db, pk)  # 앞선 같은 키 요청이 확정된 뒤에 기록을 본다
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
