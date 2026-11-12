"""이벤트 멱등 키 재생과 원천 이벤트 ID 중복 응답을 함께 확인한다."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import Event
from app.seed import DEV_A_CAMERA, ORG_A, SES_A
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration
PATH = "/v1/events"
EVENT = {
    "schema_version": "1.0.0", "session_id": SES_A, "source_system_id": DEV_A_CAMERA,
    "event_code": "VESSEL", "source": "DEVICE", "source_event_id": "synthetic-source-event",
    "started_at": "2026-11-01T03:00:00Z", "timestamp_quality": {"source": "DEVICE", "verified": False},
    "note": "합성 이벤트",
}


def test_event_creation_replays_with_same_key_and_rejects_changed_note(ctx: Ctx) -> None:
    headers = {**ctx.as_user(102), "Idempotency-Key": "synthetic-event-first"}
    first = ctx.client.post(PATH, headers=headers, json=EVENT)
    assert first.status_code == 201, first.text
    assert "Idempotent-Replayed" not in first.headers
    check(first, "POST", PATH)

    replay = ctx.client.post(PATH, headers=headers, json=EVENT)
    conflict = ctx.client.post(PATH, headers=headers, json={**EVENT, "note": "바뀐 합성 메모"})
    assert (replay.status_code, replay.headers.get("Idempotent-Replayed"), conflict.status_code) == (201, "true", 409)
    assert replay.json() == first.json()
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    check(replay, "POST", PATH)
    check(conflict, "POST", PATH)


def test_event_duplicate_with_new_key_replays_existing_source_response(ctx: Ctx) -> None:
    headers = ctx.as_user(102)
    first = ctx.client.post(PATH, headers={**headers, "Idempotency-Key": "synthetic-event-first"}, json=EVENT)
    assert first.status_code == 201, first.text
    unkeyed = ctx.client.post(PATH, headers=headers, json=EVENT)
    keyed_headers = {**headers, "Idempotency-Key": "synthetic-event-duplicate"}
    duplicate = ctx.client.post(PATH, headers=keyed_headers, json=EVENT)
    for response in (unkeyed, duplicate):
        assert response.status_code == 200, response.text
        assert response.json() == first.json()
        assert "Idempotent-Replayed" not in response.headers
        check(response, "POST", PATH)

    replay = ctx.client.post(PATH, headers=keyed_headers, json=EVENT)
    assert replay.status_code == 200, replay.text
    assert replay.headers.get("Idempotent-Replayed") == "true"
    assert replay.json() == duplicate.json()
    check(replay, "POST", PATH)

    with ctx.c.sessionmaker() as db:
        event_ids = db.scalars(select(Event.event_id).where(
            Event.organization_id == ORG_A, Event.source_system_id == DEV_A_CAMERA,
            Event.source_event_id == EVENT["source_event_id"],
        )).all()
    assert event_ids == [first.json()["event_id"]]
