"""03번 기관 하위 이벤트 코드 등록·격리·재요청 검증."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Any

import pytest
from httpx import Response
from sqlalchemy import select

from app.db.models import AuditLog, IdempotencyRecord, OrgEventCode
from app.seed import ORG_A, ORG_B, SES_A, SES_B, fid
from tests.conftest import Ctx, in_future
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_identity import _enroll, _token

pytestmark = pytest.mark.integration
TABLE = "/v1/codes/event-categories"
REGISTER = f"{TABLE}/subcodes"
CODE = "VESSEL.test_research_boat"
CREATE = {"code": CODE, "label": "합성 조사선"}
ITEM = {**CREATE, "organization_specific": True}


def assert_error(response: Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    check(response, "POST", REGISTER)
    assert response.json()["code"] == code


def test_only_human_org_admin_can_register(ctx: Ctx) -> None:
    assert_error(ctx.client.post(REGISTER, json=CREATE), 401, "UNAUTHENTICATED")
    for user in (102, 105, 106):
        response = ctx.client.post(REGISTER, headers=ctx.as_user(user, mfa=True), json=CREATE)
        assert_error(response, 403, "FORBIDDEN")

    key = ctx.client.post("/v1/api-keys", headers=ctx.as_user(101, mfa=True), json={
        "name": "subcode-test", "scopes": ["status:read"], "expires_at": in_future(days=1),
    })
    assert key.status_code == 201
    for credentials, kind in ((_enroll(ctx), "DEVICE"), (key.json(), "API_KEY")):
        token = _token(ctx, credentials["client_id"], credentials["client_secret"])
        assert token.status_code == 200
        headers = {"Authorization": "Bearer " + token.json()["access_token"]}
        assert ctx.client.get("/v1/me", headers=headers).json()["principal_kind"] == kind
        assert_error(ctx.client.post(REGISTER, headers=headers, json=CREATE), 403, "FORBIDDEN")

    assert "ORG_EVENT_CODE_CREATED" not in ctx.audit_actions()
    response = ctx.client.post(REGISTER, headers=ctx.as_user(101), json=CREATE)
    assert response.status_code == 201, response.text
    check(response, "POST", REGISTER)
    assert response.json() == ITEM


def test_registration_validation_and_scope(ctx: Ctx) -> None:
    headers = ctx.as_user(101)
    invalid_bodies = [
        {"code": CODE},
        {"label": CREATE["label"]},
        {**CREATE, "code": "VESSEL"},
        {**CREATE, "code": "VESSEL.Test"},
        {**CREATE, "code": "VESSEL.test\n"},
        {**CREATE, "code": "VESSEL." + "x" * 41},
        {**CREATE, "label": ""},
        {**CREATE, "label": " \u3000 "},
        {**CREATE, "label": "a\x00b"},
        {**CREATE, "label": "a\x7fb"},
        {**CREATE, "label": "x" * 121},
        {**CREATE, "label": 123},
        {**CREATE, "organization_id": ORG_B},
    ]
    for body in invalid_bodies:
        assert_error(ctx.client.post(REGISTER, headers=headers, json=body), 400, "VALIDATION_ERROR")
    for key in ("x" * 7, "x" * 129):
        response = ctx.client.post(REGISTER, headers={**headers, "Idempotency-Key": key}, json=CREATE)
        assert_error(response, 400, "VALIDATION_ERROR")
    response = ctx.client.post(REGISTER, headers={**headers, "X-Organization-Id": ORG_B}, json=CREATE)
    assert_error(response, 400, "VALIDATION_ERROR")
    response = ctx.client.post(REGISTER, headers=headers, params={"organization_id": ORG_B}, json=CREATE)
    assert_error(response, 400, "VALIDATION_ERROR")

    response = ctx.client.post(REGISTER, headers=headers, json={**CREATE, "code": "UNREGISTERED.test"})
    assert_error(response, 422, "RULE_VIOLATION")
    with ctx.c.sessionmaker() as db:
        assert db.scalars(select(OrgEventCode)).all() == []
    assert "ORG_EVENT_CODE_CREATED" not in ctx.audit_actions()


def test_subcode_is_visible_and_usable_only_in_own_org(ctx: Ctx) -> None:
    a_member, b_member = ctx.as_user(102), ctx.as_user(104)
    shared_key = {"Idempotency-Key": "x" * 128}
    common = ctx.client.get(TABLE, headers=a_member).json()

    a = ctx.client.post(REGISTER, headers={**ctx.as_user(101), **shared_key}, json=CREATE)
    assert a.status_code == 201, a.text
    check(a, "POST", REGISTER)
    assert a.json() == ITEM
    assert ctx.client.get(TABLE, headers=a_member).json() == {**common, "items": [*common["items"], ITEM]}
    assert ctx.client.get(TABLE, headers=b_member).json() == common

    started = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    event = {"schema_version": "1.0.0", "session_id": SES_A, "event_code": CODE,
             "started_at": started, "timestamp_quality": {"source": "DEVICE", "verified": False},
             "source": "MANUAL"}
    accepted = ctx.client.post("/v1/events", headers=a_member, json=event)
    assert accepted.status_code == 201, accepted.text
    check(accepted, "POST", "/v1/events")
    event_b = {**event, "session_id": SES_B}
    rejected = ctx.client.post("/v1/events", headers=b_member, json=event_b)
    assert rejected.status_code == 422, rejected.text
    check(rejected, "POST", "/v1/events")
    assert rejected.json()["code"] == "RULE_VIOLATION"

    b = ctx.client.post(REGISTER, headers={**ctx.as_user(103), **shared_key}, json={**CREATE, "label": "합성 실증선"})
    assert b.status_code == 201, b.text
    check(b, "POST", REGISTER)
    assert "idempotent-replayed" not in b.headers
    b_item = {**ITEM, "label": "합성 실증선"}
    assert b.json() == b_item
    assert ctx.client.get(TABLE, headers=a_member).json() == {**common, "items": [*common["items"], ITEM]}
    assert ctx.client.get(TABLE, headers=b_member).json() == {**common, "items": [*common["items"], b_item]}
    accepted = ctx.client.post("/v1/events", headers=b_member, json=event_b)
    assert accepted.status_code == 201, accepted.text
    check(accepted, "POST", "/v1/events")


def test_concurrent_registration_replays_once_and_audits_once(ctx: Ctx) -> None:
    admin = ctx.as_user(101)
    headers = {**admin, "Idempotency-Key": "race-key"}
    ready = Barrier(2, timeout=10)

    def submit() -> Response:
        ready.wait()
        return ctx.client.post(REGISTER, headers=headers, json=CREATE)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = [pool.submit(submit) for _ in range(2)]
        responses = [first.result(), second.result()]

    for response in responses:
        assert response.status_code == 201, response.text
        check(response, "POST", REGISTER)
        assert response.json() == ITEM
    assert {r.headers.get("idempotent-replayed") for r in responses} == {None, "true"}

    changed = {**CREATE, "label": "바뀐 이름"}
    assert_error(ctx.client.post(REGISTER, headers=headers, json=changed), 409, "IDEMPOTENCY_CONFLICT")
    assert_error(ctx.client.post(REGISTER, headers=admin, json=changed), 409, "CONFLICT")

    with ctx.c.sessionmaker() as db:
        stored = db.scalars(select(OrgEventCode)).one()
        assert (stored.organization_id, stored.code, stored.label, stored.created_by) == (
            ORG_A, CODE, CREATE["label"], fid("usr", 101),
        )
        audit = db.scalars(select(AuditLog).where(AuditLog.action == "ORG_EVENT_CODE_CREATED")).one()
        assert (audit.organization_id, audit.actor_id, audit.actor_kind, audit.result) == (
            ORG_A, fid("usr", 101), "USER", "SUCCESS",
        )
        assert audit.target_type == "OrgEventCode" and audit.target_id is None
        assert audit.details["code"] == CODE


def test_registration_audit_and_idempotency_roll_back_together(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    key = "rollback-key"
    headers = {**ctx.as_user(101), "Idempotency-Key": key}
    original_record = ctx.c.audit.record
    audit_written = False

    def record_then_fail(action: str, **kwargs: Any) -> str:
        nonlocal audit_written
        audit_id = original_record(action, **kwargs)
        if action == "ORG_EVENT_CODE_CREATED":
            audit_written = True
            raise RuntimeError("synthetic failure after audit insert")
        return audit_id

    with monkeypatch.context() as patch:
        patch.setattr(ctx.c.audit, "record", record_then_fail)
        failed = ctx.client.post(REGISTER, headers=headers, json=CREATE)
    assert failed.status_code == 500, failed.text
    assert audit_written

    idempotency = select(IdempotencyRecord).where(IdempotencyRecord.key == key)
    audit = select(AuditLog).where(AuditLog.action == "ORG_EVENT_CODE_CREATED")
    with ctx.c.sessionmaker() as db:
        assert db.scalars(select(OrgEventCode)).all() == []
        assert db.scalars(idempotency).all() == []
        assert db.scalars(audit).all() == []

    retried = ctx.client.post(REGISTER, headers=headers, json=CREATE)
    assert retried.status_code == 201, retried.text
    check(retried, "POST", REGISTER)
    assert retried.json() == ITEM
    assert "idempotent-replayed" not in retried.headers
    with ctx.c.sessionmaker() as db:
        assert db.scalars(select(OrgEventCode)).one().code == CODE
        assert db.scalars(idempotency).one().response_body == ITEM
        assert db.scalars(audit).one().details["code"] == CODE
