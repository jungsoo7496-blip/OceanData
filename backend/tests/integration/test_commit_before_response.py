"""지시서 5번: 요청 트랜잭션은 응답을 보내기 전에 확정하고, 같은 Idempotency-Key 동시 요청은 줄을 세운다.

전에는 get_db가 request 범위 yield 의존성이라 FastAPI(0.118 이상)가 그 마무리(커밋)를 응답을 보낸 **뒤** 돌렸다.
커밋이 실패해도 클라이언트는 이미 201을 받았고 작업은 없었다(GET → 404). 같은 키 두 요청이 모두 201을 받고 뒤 요청의 작업이
사라지기도 했다. 값은 모두 합성이다.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import app.api.context as ctx_api
import app.api.observations as obs_api
import app.services.idempotency as idem
from app.metrics import REGISTRY
from app.seed import ORG_A, PRJ_A, SES_A, fid
from app.services.idempotency import body_hash, lock_scope

pytestmark = pytest.mark.integration
MEMBER_A = 102
EVENT = {"schema_version": "1.0.0", "session_id": SES_A, "event_code": "VESSEL", "started_at": "2026-11-02T00:00:00Z",
         "timestamp_quality": {"source": "DEVICE", "verified": True}, "source": "MANUAL"}
SESSION = {"project_id": PRJ_A, "label": "2026-11-03_동시", "title": "동시 요청 확인 (합성)",
           "starts_at": "2026-11-03T00:00:00Z"}
POST_EVENTS = "POST /v1/events"


def _event_count(ctx) -> int:  # type: ignore[no-untyped-def]
    with ctx.c.engine.connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM events WHERE session_id = :s"), {"s": SES_A}).scalar_one())


def _session_count(ctx, label: str) -> int:  # type: ignore[no-untyped-def]
    with ctx.c.engine.connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM sessions WHERE label = :l"), {"l": label}).scalar_one())


def _store_key(ctx, key: str, *, h: str, status: int, body: str) -> None:  # type: ignore[no-untyped-def]
    """다른 요청이 먼저 확정한 멱등성 기록 (합성)."""
    with ctx.c.engine.begin() as conn:
        conn.execute(text("INSERT INTO idempotency_keys (organization_id, principal_id, method_path, key, body_hash, "
                          "status_code, response_body, created_at, expires_at) VALUES (:o, :p, 'POST /v1/events', "
                          ":k, :h, :s, CAST(:b AS jsonb), now(), now() + interval '1 day')"),
                     {"o": ORG_A, "p": fid("usr", MEMBER_A), "k": key, "h": h, "s": status, "b": body})


def _lock_waiters(ctx) -> int:  # type: ignore[no-untyped-def]
    with ctx.c.engine.connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                                     "AND wait_event_type = 'Lock'")).scalar_one())


def _hold_until_a_request_waits(ctx, first_inside: threading.Event, calls: list[Any]) -> None:  # type: ignore[no-untyped-def]
    """첫 요청이 트랜잭션 안에서 멈춘 채, 다른 요청이 DB 잠금을 기다리기 시작할 때까지 기다린다."""
    first_inside.set()
    deadline = time.monotonic() + 10  # 잠금이 없으면 두 번째 요청이 기다리지 않아 시간만 지난다
    while time.monotonic() < deadline and not _lock_waiters(ctx):
        time.sleep(0.05)
    calls.append("released")


def test_commit_failure_never_reaches_the_client_as_success(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """커밋이 실패하면 201이 아니라 오류 응답이다 (전: 201을 받았는데 GET → 404)."""
    real_commit, armed = Session.commit, {"on": False}

    def commit(self: Session) -> None:
        if armed["on"]:
            armed["on"] = False
            raise OperationalError("COMMIT", {}, Exception("합성 커밋 실패"))
        real_commit(self)

    monkeypatch.setattr(Session, "commit", commit)
    before = _event_count(ctx)
    armed["on"] = True
    r = ctx.client.post("/v1/events", json=EVENT, headers=ctx.as_user(MEMBER_A))
    assert r.status_code == 500, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    assert _event_count(ctx) == before  # 작업은 남지 않았다
    assert ctx.client.post("/v1/events", json=EVENT, headers=ctx.as_user(MEMBER_A)).status_code == 201  # 다음 요청은 정상


def test_commit_conflict_is_a_409_problem_and_rolls_back(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """커밋에서 고유 제약이 깨지면 201이 아니라 409 문제 응답이고, 작업은 되돌린다."""
    _store_key(ctx, "already-committed-0001", h="x", status=201, body="{}")
    real_view = obs_api.event_view
    from app.db.models import IdempotencyRecord

    def view_then_conflict(db, e):  # type: ignore[no-untyped-def]
        out = real_view(db, e)
        # flush 없이 세션에만 넣는다: 경로 함수가 끝난 뒤 커밋에서야 고유 제약 위반이 난다
        db.add(IdempotencyRecord(organization_id=ORG_A, principal_id=fid("usr", MEMBER_A),
                                 method_path="POST /v1/events", key="already-committed-0001", body_hash="y",
                                 status_code=201, response_body={}, created_at=e.created_at,
                                 expires_at=e.created_at))
        return out

    monkeypatch.setattr(obs_api, "event_view", view_then_conflict)
    before = _event_count(ctx)
    seen: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record)

    handler, errors_log = Capture(level=logging.WARNING), logging.getLogger("app.errors")
    errors_log.addHandler(handler)
    sample = {"sqlstate": "23505"}
    counted = REGISTRY.get_sample_value("db_conflict_responses_total", sample) or 0.0
    try:
        r = ctx.client.post("/v1/events", json=EVENT, headers=ctx.as_user(MEMBER_A))
    finally:
        errors_log.removeHandler(handler)
    assert r.status_code == 409, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == "CONFLICT"
    assert _event_count(ctx) == before  # 되돌렸다
    # 서버 버그로 난 제약 위반도 재시도 가능한 409로 보이므로 SQLSTATE·제약 이름을 로그와 지표로 남긴다 (검토 K4)
    mapped = [x for x in seen if x.getMessage() == "db_conflict_mapped"]
    assert len(mapped) == 1 and mapped[0].sqlstate == "23505" and mapped[0].constraint, seen
    assert not any("Key (" in str(v) for v in vars(mapped[0]).values())  # 바인딩 값이 든 DETAIL은 남기지 않는다
    assert (REGISTRY.get_sample_value("db_conflict_responses_total", sample) or 0.0) == counted + 1


def test_concurrent_same_key_requests_serialize(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """같은 키 두 요청이 동시에 들어오면 뒤 요청은 앞 요청이 확정될 때까지 기다렸다가 같은 응답을 다시 받는다."""
    member = ctx.as_user(MEMBER_A)
    key = "agent-evt-0002"
    real_view = obs_api.event_view
    first_inside = threading.Event()
    calls: list[Any] = []

    def view_wait_for_second(db, e):  # type: ignore[no-untyped-def]
        calls.append(e.event_id)
        if len(calls) == 1:
            _hold_until_a_request_waits(ctx, first_inside, calls)
        return real_view(db, e)

    monkeypatch.setattr(obs_api, "event_view", view_wait_for_second)
    before = _event_count(ctx)

    def post() -> Any:
        return ctx.client.post("/v1/events", json=EVENT, headers={**member, "Idempotency-Key": key})

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(post)
        assert first_inside.wait(10)
        second = pool.submit(post)
        r1, r2 = first.result(timeout=60), second.result(timeout=60)
    assert (r1.status_code, r2.status_code) == (201, 201), (r1.text, r2.text)
    assert r1.headers.get("Idempotent-Replayed") is None
    assert r2.headers.get("Idempotent-Replayed") == "true"
    assert r1.json()["event_id"] == r2.json()["event_id"]
    assert [c for c in calls if c != "released"].__len__() == 1  # 작업은 한 번만
    assert _event_count(ctx) == before + 1
    assert ctx.client.get(f"/v1/events/{r1.json()['event_id']}", headers=member).status_code == 200


def test_concurrent_same_key_session_creates_make_one_session(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """계약 점검 20번: POST /v1/sessions도 같은 키 동시 요청에 세션을 하나만 만들고 뒤 요청은 같은 응답을 받는다."""
    member, key = ctx.as_user(MEMBER_A), "agent-ses-0001"
    real_view = ctx_api.session_view
    first_inside = threading.Event()
    calls: list[Any] = []

    def view_wait_for_second(s, device_ids):  # type: ignore[no-untyped-def]
        calls.append(s.session_id)
        if len(calls) == 1:
            _hold_until_a_request_waits(ctx, first_inside, calls)
        return real_view(s, device_ids)

    monkeypatch.setattr(ctx_api, "session_view", view_wait_for_second)

    def post() -> Any:
        return ctx.client.post("/v1/sessions", json=SESSION, headers={**member, "Idempotency-Key": key})

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(post)
        assert first_inside.wait(10)
        second = pool.submit(post)
        r1, r2 = first.result(timeout=60), second.result(timeout=60)
    assert (r1.status_code, r2.status_code) == (201, 201), (r1.text, r2.text)  # 전: 뒤 요청이 label 충돌 409
    assert r2.headers.get("Idempotent-Replayed") == "true" and r1.json() == r2.json()
    assert _session_count(ctx, SESSION["label"]) == 1


def test_waiting_for_a_slow_first_request_is_bounded(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """앞 요청이 오래 걸리면 뒤 요청은 무한정 기다리지 않고 409(곧 다시)를 받는다. 앞 요청이 끝나면 같은 키로 다시 보낼 수 있다."""
    monkeypatch.setattr(idem, "LOCK_WAIT_MS", 300)
    key = "agent-evt-0003"
    scope = lock_scope(ORG_A, fid("usr", MEMBER_A), POST_EVENTS, key)
    with ctx.c.engine.connect() as holder:  # 앞 요청 역할: 같은 키 잠금을 쥐고 있다
        holder.execute(text("SELECT pg_advisory_lock(hashtextextended(:s, 0))"), {"s": scope})
        started = time.monotonic()
        r = ctx.client.post("/v1/events", json=EVENT, headers={**ctx.as_user(MEMBER_A), "Idempotency-Key": key})
        waited = time.monotonic() - started
        assert r.status_code == 409 and r.json()["code"] == "CONFLICT", r.text
        assert r.headers.get("Retry-After") == "1"
        assert 0.25 <= waited < 5, waited
        holder.execute(text("SELECT pg_advisory_unlock(hashtextextended(:s, 0))"), {"s": scope})
        holder.commit()
    before = _event_count(ctx)
    r = ctx.client.post("/v1/events", json=EVENT, headers={**ctx.as_user(MEMBER_A), "Idempotency-Key": key})
    assert r.status_code == 201 and _event_count(ctx) == before + 1


def test_waiters_beyond_the_cap_get_an_immediate_409(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """같은 키를 기다릴 수 있는 요청 수에 상한이 있다: 넘치는 요청은 기다리지 않고 바로 409(곧 다시)를 받는다.
    기다리는 요청은 스레드와 DB 연결을 하나씩 쥐므로, 재시도 폭주가 풀을 다 쓰고 /healthz까지 늦추지 않게 한다 (검토 K1)."""
    monkeypatch.setattr(idem, "_waiters", threading.BoundedSemaphore(1))
    monkeypatch.setattr(idem, "LOCK_WAIT_MS", 5_000)
    key = "agent-evt-0005"
    scope = lock_scope(ORG_A, fid("usr", MEMBER_A), POST_EVENTS, key)

    def post() -> Any:
        return ctx.client.post("/v1/events", json=EVENT, headers={**ctx.as_user(MEMBER_A), "Idempotency-Key": key})

    with ctx.c.engine.connect() as holder:
        holder.execute(text("SELECT pg_advisory_lock(hashtextextended(:s, 0))"), {"s": scope})
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(post)  # 첫 대기자가 자리를 차지하고 기다린다
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not _lock_waiters(ctx):
                time.sleep(0.05)
            assert _lock_waiters(ctx) == 1
            started = time.monotonic()
            second = post()  # 자리가 없다
            assert time.monotonic() - started < 1.5
            assert second.status_code == 409 and second.json()["code"] == "CONFLICT", second.text
            assert second.headers.get("Retry-After") == "1"
            holder.execute(text("SELECT pg_advisory_unlock(hashtextextended(:s, 0))"), {"s": scope})
            holder.commit()
            assert first.result(timeout=30).status_code == 201  # 기다리던 요청은 앞 요청이 풀리자 처리된다


def test_a_request_that_fails_does_not_keep_the_key(ctx) -> None:  # type: ignore[no-untyped-def]
    """오류로 끝난 요청은 키를 쥐지 않는다: 같은 키로 고쳐 보내면 새로 처리한다 (잠금도 풀렸다)."""
    member, key = ctx.as_user(MEMBER_A), "agent-ses-0002"
    assert ctx.client.post("/v1/sessions", json=SESSION, headers=member).status_code == 201
    r = ctx.client.post("/v1/sessions", json=SESSION, headers={**member, "Idempotency-Key": key})
    assert r.status_code == 409, r.text  # 같은 label 충돌 — 이 요청은 기록을 남기지 않는다
    fixed = {**SESSION, "label": "2026-11-03_고침"}
    r = ctx.client.post("/v1/sessions", json=fixed, headers={**member, "Idempotency-Key": key})
    assert r.status_code == 201 and r.headers.get("Idempotent-Replayed") is None
    r = ctx.client.post("/v1/sessions", json=fixed, headers={**member, "Idempotency-Key": key})
    assert r.status_code == 201 and r.headers.get("Idempotent-Replayed") == "true"
    r = ctx.client.post("/v1/sessions", json=SESSION, headers={**member, "Idempotency-Key": key})
    assert r.status_code == 409 and r.json()["code"] == "IDEMPOTENCY_CONFLICT"  # 같은 키·다른 본문


def test_existing_record_is_replayed_without_running_the_work(ctx) -> None:  # type: ignore[no-untyped-def]
    """확정된 기록이 있으면(잠금을 기다릴 필요 없이) 그대로 답하고 작업은 하지 않는다."""
    key = "agent-evt-0004"
    stored = '{"event_id": "evt_01JC0000000000000000000000"}'
    _store_key(ctx, key, h=body_hash(EVENT), status=201, body=stored)
    before = _event_count(ctx)
    r = ctx.client.post("/v1/events", json=EVENT, headers={**ctx.as_user(MEMBER_A), "Idempotency-Key": key})
    assert r.status_code == 201 and r.headers.get("Idempotent-Replayed") == "true"
    assert r.json() == {"event_id": "evt_01JC0000000000000000000000"} and _event_count(ctx) == before
