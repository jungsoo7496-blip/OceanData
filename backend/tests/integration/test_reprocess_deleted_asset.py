"""재처리 요청과 겹쳐 확정된 삭제 (통합 검토 ASSETS-4, ADR-0076 보완).

재처리는 자산을 잠그지 않고 읽은 뒤(load_scoped는 삭제된 자산을 404로 거른다) 행을 잠그고 다시 읽어 판단한다.
그사이 삭제가 확정되면 다시 읽은 행에는 deleted_at이 있는데 그것을 보지 않아, 지워진 자산에 202·처리 작업·
REPROCESS_REQUESTED SUCCESS 감사가 남고 지워진 자산과 파생본에 '재처리됨' 시각이 찍혔다. 다른 요청은 모두 404를 준다.
시험 데이터는 합성 값이다.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from app.api import assets as assets_api
from app.db.models import Asset, AuditLog, JobRow
from app.seed import DEV_A_CAMERA, ORG_A, PRJ_A, fid
from app.services import retention
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract
from tests.integration.test_catalog import _asset

pytestmark = pytest.mark.integration
ADMIN, OP = 101, 106


def test_reprocess_of_an_asset_deleted_before_the_lock_is_not_found(ctx: Ctx, monkeypatch) -> None:
    with ctx.c.sessionmaker.begin() as db:
        a = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)  # READY_PRIVATE 원 영상, 바이너리 있음
        db.add(a)
        aid = a.asset_id
    real_load, deleted = assets_api.load_scoped, []

    def load_then_delete(db: Any, c: Any, request: Any, model: Any, ident: str, principal: Any, **kw: Any) -> Any:
        row = real_load(db, c, request, model, ident, principal, **kw)  # 재처리가 잠그지 않고 살아 있는 자산을 읽었다
        if model is Asset and not deleted:
            deleted.append(ident)
            with ctx.c.sessionmaker.begin() as other:  # 삭제 요청이 다른 연결에서 확정됐다 (삭제 경로와 같은 잠금·기록)
                roots = retention.with_descendants(other, [other.get(Asset, aid)], lock=True)
                retention.delete_assets(ctx.c, other, organization_id=ORG_A, assets=roots, scope="ASSET",
                                        target_id=aid, reason="합성 삭제", requested_by=fid("usr", ADMIN),
                                        confirm_affected=False)
        return row

    monkeypatch.setattr(assets_api, "load_scoped", load_then_delete)
    r = ctx.client.post(f"/v1/assets/{aid}/reprocess", json={"steps": ["META"], "reason": "겹친 재처리 요청"},
                        headers=ctx.as_user(OP, mfa=True))
    assert deleted == [aid]
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "NOT_FOUND"
    assert_matches_contract(r, "POST", "/v1/assets/{asset_id}/reprocess")
    with ctx.c.sessionmaker() as db:
        asset = db.get(Asset, aid)
        assert asset is not None and asset.deleted_at is not None
        assert asset.last_reprocessed_at is None, "지워진 자산에 재처리 시각이 찍혔다"
        jobs = db.scalars(select(JobRow.job_type).where(JobRow.asset_id == aid)).all()
        audits = db.scalars(select(AuditLog.action).where(AuditLog.target_id == aid,
                                                          AuditLog.action == "REPROCESS_REQUESTED")).all()
    assert (jobs, audits) == ([], []), "지워진 자산에 처리 작업이나 재처리 감사가 남았다"
