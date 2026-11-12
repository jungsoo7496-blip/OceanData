"""외부 소스 PATCH의 생략·명시적 null·호출 한도 해제를 확인한다."""

from __future__ import annotations

from typing import Any

import pytest

from tests.acceptance.test_t17_env_join import OP, SOURCE
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration

LIST_PATH = "/v1/external-sources"
PATCH_PATH = LIST_PATH + "/{source_id}"


@pytest.fixture()
def mock_source(ctx: Ctx) -> dict[str, Any]:
    response = ctx.client.post(LIST_PATH, json={**SOURCE, "rate_limit_per_min": 7}, headers=ctx.as_user(OP))
    assert response.status_code == 201, response.text
    return response.json()


def _stored_source(ctx: Ctx, source_id: str) -> dict[str, Any]:
    response = ctx.client.get(LIST_PATH, headers=ctx.as_user(OP))
    assert response.status_code == 200, response.text
    return next(source for source in response.json()["items"] if source["source_id"] == source_id)


@pytest.mark.parametrize("field", ["enabled", "interval_s", "attribution", "redistribution_allowed"])
def test_external_source_patch_rejects_null_without_applying_other_changes(
    ctx: Ctx, mock_source: dict[str, Any], field: str,
) -> None:
    source_id = mock_source["source_id"]
    response = ctx.client.patch(PATCH_PATH.format(source_id=source_id), headers=ctx.as_user(OP),
                                json={field: None, "rate_limit_per_min": 9})
    assert response.status_code == 400, response.text
    check(response, "PATCH", PATCH_PATH)
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert f"/{field}" in {error["pointer"] for error in body["errors"]}
    assert _stored_source(ctx, source_id) == mock_source
    assert "EXTERNAL_SOURCE_UPDATED" not in ctx.audit_actions()


def test_external_source_patch_clears_nullable_rate_limit_and_preserves_omitted_fields(
    ctx: Ctx, mock_source: dict[str, Any],
) -> None:
    source_id = mock_source["source_id"]
    changes = {"enabled": False, "rate_limit_per_min": None}
    response = ctx.client.patch(PATCH_PATH.format(source_id=source_id), headers=ctx.as_user(OP), json=changes)
    assert response.status_code == 200, response.text
    check(response, "PATCH", PATCH_PATH)
    expected = {**mock_source, **changes}
    assert response.json() == expected
    assert _stored_source(ctx, source_id) == expected
