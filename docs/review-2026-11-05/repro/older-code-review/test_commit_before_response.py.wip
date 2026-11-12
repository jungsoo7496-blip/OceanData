"""검토 2 (커밋 시점): 요청 트랜잭션은 응답을 보내기 전에 확정하고, 같은 Idempotency-Key 동시 요청은 줄을 세운다.

전에는 get_db가 request 범위 yield 의존성이라 커밋이 응답을 보낸 뒤에 돌았다. 커밋이 실패하면 클라이언트는 이미
201을 받았는데 이벤트는 없었다 (GET → 404). 같은 키 두 요청이 모두 201을 받고 뒤 요청의 작업이 사라지기도 했다.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from sqlalchemy import text

import app.api.observations as obs_api
from app.seed import ORG_A, SES_A, fid
from app.services.idempotency import body_hash

pytestmark = pytest.mark.integration
MEMBER_A = 102
EVENT = {"schema_version": "1.0.0", "session_id": SES_A, "event_code": "VESSEL", "started_at": "2026-11-02T00:00:00Z",
         "timestamp_quality": {"source": "DEVICE", "verified": True}, "source": "MANUAL"}


def _event_count(ctx) -> int:  # type: ignore[no-untyped-def]
    with ctx.c.engine.connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM events WHERE session_id = :s"), {"s": SES_A}).scalar_one())


def _store_key(ctx, key: str, *, h: str, status: int, body: str) -> None:  # type: ignore[no-untyped-def]
    """잠금을 거치지 않고 먼저 확정한 다른 요청의 멱등성 기록 (합성)."""
    with ctx.c.engine.begin() as conn:
        conn.execute(text("INSERT INTO idempotency_keys (organization_id, principal_id, method_path, key, body_hash, "
                          "status_code, response_body, created_at, expires_at) VALUES (:o, :p, 'POST /v1/events', "
                          ":k, :h, :s, CAST(:b AS jsonb), now(), now() + interval '1 day')"),
                     {"o": ORG_A, "p": fid("usr", MEMBER_A), "k": key, "h": h, "s": status, "b": body})


def test_commit_failure_is_reported_before_response(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
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
    r = ctx.client.post("/v1/events", json=EVENT, headers=ctx.as_user(MEMBER_A))
    assert r.status_code == 409, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == "CONFLICT"
    assert _event_count(ctx) == before  # 되돌렸다


@pytest.mark.parametrize("same_body", [True, False])
def test_racing_same_key_writer_wins(ctx, monkeypatch, same_body: bool) -> None:  # type: ignore[no-untyped-def]
    """잠금 밖에서 같은 키 기록이 먼저 확정되면 이 요청은 되돌리고 그 기록대로 답한다 (재현: 201인데 GET 404)."""
    member = ctx.as_user(MEMBER_A)
    key = "agent-evt-0001"
    stored = '{"event_id": "evt_01JC0000000000000000000000"}'
    real_view = obs_api.event_view

    def view_and_race(db, e):  # type: ignore[no-untyped-def]
        _store_key(ctx, key, h=body_hash(EVENT) if same_body else "x", status=201, body=stored)
        return real_view(db, e)

    monkeypatch.setattr(obs_api, "event_view", view_and_race)
    before = _event_count(ctx)
    r = ctx.client.post("/v1/events", json=EVENT, headers={**member, "Idempotency-Key": key})
    if same_body:
        assert r.status_code == 201, r.text
        assert r.headers.get("Idempotent-Replayed") == "true"
        assert r.json() == {"event_id": "evt_01JC0000000000000000000000"}
    else:
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert _event_count(ctx) == before  # 이 요청이 만든 이벤트는 남지 않는다


def test_concurrent_same_key_requests_serialize(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """같은 키 두 요청이 동시에 들어오면 뒤 요청은 앞 요청이 확정될 때까지 기다렸다가 같은 응답을 다시 받는다."""
    member = ctx.as_user(MEMBER_A)
    key = "agent-evt-0002"
    real_view = obs_api.event_view
    first_inside = threading.Event()
    calls: list[str] = []

    def view_wait_for_second(db, e):  # type: ignore[no-untyped-def]
        calls.append(e.event_id)
        if len(calls) == 1:
            first_inside.set()
            deadline = time.monotonic() + 10  # 잠금이 없으면 두 번째 요청이 기다리지 않아 여기서 시간만 지난다
            while time.monotonic() < deadline:
                with ctx.c.engine.connect() as conn:
                    waiting = conn.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname = "
                                                "current_database() AND wait_event_type = 'Lock'")).scalar_one()
                if waiting:
                    break
                time.sleep(0.05)
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
    assert len(calls) == 1  # 작업은 한 번만
    assert _event_count(ctx) == before + 1
    assert ctx.client.get(f"/v1/events/{r1.json()['event_id']}", headers=member).status_code == 200
