"""겹치는 자산 조회는 이벤트와 자산의 읽기 권한을 모두 확인한다."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.seed import SES_A
from tests.conftest import Ctx, in_future
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_device_reach import _device_headers
from tests.integration.test_event_overlap import T, _put, z

pytestmark = pytest.mark.integration
PATH = "/v1/events/{event_id}/overlapping-assets"


@pytest.fixture()
def overlap_event(ctx: Ctx) -> tuple[str, str]:
    event = ctx.client.post("/v1/events", headers=ctx.as_user(102), json={
        "schema_version": "1.0.0", "session_id": SES_A, "event_code": "SAFETY_INCIDENT", "source": "MANUAL",
        "severity": "WARNING", "started_at": z(T), "ended_at": z(T + timedelta(minutes=5)),
        "timestamp_quality": {"source": "DEVICE", "verified": False},
    })
    assert event.status_code == 201, event.text
    asset_id = _put(ctx, T + timedelta(minutes=1), T + timedelta(minutes=2))
    return event.json()["event_id"], asset_id


def test_overlap_api_key_requires_event_and_asset_scopes(ctx: Ctx, overlap_event: tuple[str, str]) -> None:
    event_id, asset_id = overlap_event
    path = PATH.format(event_id=event_id)
    event_path = f"/v1/events/{event_id}"
    asset_path = f"/v1/assets/{asset_id}"
    issued = ctx.client.post("/v1/api-keys", headers=ctx.as_user(101, mfa=True), json={
        "name": "synthetic-overlap-reader", "scopes": ["status:read", "asset:read"], "expires_at": in_future(days=1),
    })
    assert issued.status_code == 201
    credentials = issued.json()

    def token_headers(scope: str) -> dict[str, str]:
        token = ctx.client.post("/v1/auth/token", data={
            "grant_type": "client_credentials", "client_id": credentials["client_id"],
            "client_secret": credentials["client_secret"], "scope": scope,
        })
        assert token.status_code == 200
        return {"Authorization": "Bearer " + token.json()["access_token"]}

    for headers in (ctx.as_user(102), token_headers("status:read asset:read")):
        response = ctx.client.get(path, headers=headers)
        assert response.status_code == 200, response.text
        check(response, "GET", PATH)
        assert [item["asset"]["asset_id"] for item in response.json()["items"]] == [asset_id]

    asset_only = token_headers("asset:read")
    assert ctx.client.get(asset_path, headers=asset_only).status_code == 200
    assert ctx.client.get(path, headers=asset_only).status_code == 403

    status_only = token_headers("status:read")
    assert ctx.client.get(event_path, headers=status_only).status_code == 200
    assert ctx.client.get(asset_path, headers=status_only).status_code == 403
    denied = ctx.client.get(path, headers=status_only)
    assert denied.status_code == 403
    assert denied.json()["code"] == "FORBIDDEN"
    check(denied, "GET", PATH)


def test_overlap_remains_unavailable_to_collector(ctx: Ctx, overlap_event: tuple[str, str]) -> None:
    event_id, _ = overlap_event
    denied = ctx.client.get(PATH.format(event_id=event_id), headers=_device_headers(ctx))
    assert denied.status_code == 403
    assert denied.json()["code"] == "FORBIDDEN"
    check(denied, "GET", PATH)
