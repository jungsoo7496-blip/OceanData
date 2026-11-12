"""인증 실패는 요청마다 감사하되 입력한 경로와 자격증명을 남기지 않는다."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.db.models import AuditLog
from tests.conftest import Ctx

pytestmark = pytest.mark.integration


def test_auth_failures_each_audit_only_the_route_template(ctx: Ctx) -> None:
    raw_id = "synthetic-path-" + "x" * 200
    invalid_bearer = "synthetic-invalid-bearer"
    query_value = "synthetic-query-value"
    request_ids = []
    for headers in ({}, {"Authorization": f"Bearer {invalid_bearer}"}) * 3:
        response = ctx.client.get(f"/v1/assets/{raw_id}", headers=headers, params={"q": query_value})
        assert response.status_code == 401 and response.json()["code"] == "UNAUTHENTICATED"
        assert response.headers["WWW-Authenticate"] == "Bearer"
        request_ids.append(response.headers["x-request-id"])

    assert len(set(request_ids)) == 6
    with ctx.c.sessionmaker() as db:
        rows = db.scalars(select(AuditLog).where(AuditLog.action == "AUTH_FAILED").order_by(AuditLog.seq)).all()
        assert [(row.request_id, row.result) for row in rows] == [(rid, "DENIED") for rid in request_ids]
        details = [row.details for row in rows]
    assert ctx.c.audit.verify_chain().ok
    encoded = json.dumps(details)
    assert invalid_bearer not in encoded and query_value not in encoded
    assert [detail["path"] for detail in details] == ["/v1/assets/{asset_id}"] * len(request_ids)
