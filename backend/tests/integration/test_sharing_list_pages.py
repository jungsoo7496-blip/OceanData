"""이용 신청·이용 권한·이용 이력 목록은 limit·cursor로 나눈다 (통합 검토 SHARING-3, api-conventions.md, ADR-0088).

전에는 세 목록 모두 쌓인 행을 한 응답에 모두 담았고, 이용 이력은 신청마다 결정·권한·다운로드를 따로 읽었다.
합성 행만 쓴다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event

from app.db.models import AccessDecision, AccessRequest, Dataset, DatasetVersion, Distribution, Grant
from app.domain.ids import new_id
from app.seed import ORG_A, ORG_CONSUMER
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration
ADMIN_A, OP, CONSUMER = 101, 106, 109
T0 = datetime(2026, 11, 20, 1, 0, tzinfo=UTC)


def _version(db: Any) -> str:
    did, vid = new_id("dataset"), new_id("dataset_version")
    db.add(Dataset(dataset_id=did, title="합성 데이터셋", created_by="t", created_at=T0))
    db.flush()
    db.add(DatasetVersion(dataset_version_id=vid, dataset_id=did, version="1.0.0", provider_organization_ids=[ORG_A],
                          item_count=1, total_bytes=10, data_card={}, manifest={}, manifest_key=f"{did}/m.json",
                          manifest_sha256="0" * 64, created_by="t", created_at=T0))
    db.flush()
    return vid


def _request(db: Any, vid: str, at: datetime, status: str = "REQUESTED") -> str:
    rid = new_id("access_request")
    db.add(AccessRequest(access_request_id=rid, dataset_version_id=vid, organization_id=ORG_CONSUMER,
                         requested_by="t", purpose="AI_TRAINING", purpose_detail=f"합성 신청 {at:%H:%M:%S}",
                         period_start=at, period_end=at + timedelta(days=30), security_measures="합성",
                         status=status, deadline_at=at + timedelta(days=7), created_at=at, updated_at=at))
    db.flush()
    return rid


def _grant(db: Any, rid: str, vid: str, at: datetime, status: str = "GRANTED") -> str:
    gid = new_id("grant")
    db.add(Grant(grant_id=gid, access_request_id=rid, dataset_version_id=vid, organization_id=ORG_CONSUMER,
                 status=status, starts_at=at, expires_at=at + timedelta(days=30),
                 revoked_at=at + timedelta(hours=2) if status == "REVOKED" else None,
                 revoke_reason="합성 철회" if status == "REVOKED" else None, created_at=at))
    db.flush()
    return gid


def _pages(ctx: Ctx, path: str, user: int, limit: int) -> list[list[dict]]:
    pages: list[list[dict]] = []
    cursor = None
    while True:
        params: dict[str, str | int] = {"limit": limit, **({"cursor": cursor} if cursor else {})}
        r = ctx.client.get(path, params=params, headers=ctx.as_user(user))
        assert r.status_code == 200, r.text
        check(r, "GET", path)
        pages.append(r.json()["items"])
        cursor = r.json()["next_cursor"]
        if cursor is None:
            return pages
        assert len(pages) < 100


@pytest.mark.parametrize(("path", "key"), [("/v1/access-requests", "access_request_id"), ("/v1/grants", "grant_id")])
def test_requests_and_grants_page_by_limit_and_cursor(ctx: Ctx, path: str, key: str) -> None:
    with ctx.c.sessionmaker.begin() as db:
        vid = _version(db)
        rids = [_request(db, vid, T0 + timedelta(seconds=i), "GRANTED") for i in range(51)]
        gids = [_grant(db, rid, vid, T0 + timedelta(seconds=i)) for i, rid in enumerate(rids)]
    ids = sorted(rids if key == "access_request_id" else gids)

    r = ctx.client.get(path, headers=ctx.as_user(OP))  # 기본 50
    check(r, "GET", path)
    assert len(r.json()["items"]) == 50 and r.json()["next_cursor"], r.text

    for user in (OP, CONSUMER, ADMIN_A):  # 운영자·이용기관·제공기관 모두 같은 규칙으로 나뉜다
        pages = _pages(ctx, path, user, 20)
        assert [len(p) for p in pages] == [20, 20, 11]
        assert [x[key] for p in pages for x in p] == ids  # 겹치지도 빠지지도 않는다
    assert ctx.client.get(f"{path}?limit=0", headers=ctx.as_user(OP)).status_code == 400
    assert ctx.client.get(f"{path}?limit=501", headers=ctx.as_user(OP)).status_code == 400


def _history_rows(ctx: Ctx, n: int) -> int:
    """신청 n개(결정 1개씩), 둘째마다 권한·다운로드 2회, 셋째마다 철회. 만든 이력 사건 수를 돌려준다."""
    events = 0
    with ctx.c.sessionmaker.begin() as db:
        vid = _version(db)
        for i in range(n):
            at = T0 + timedelta(minutes=i)
            rid = _request(db, vid, at, "GRANTED" if i % 2 == 0 else "REQUESTED")
            db.add(AccessDecision(access_request_id=rid, organization_id=ORG_A, role="PROVIDER", decision="APPROVE",
                                  reason="합성 승인", decided_by="t", decided_at=at + timedelta(seconds=30)))
            events += 2
            if i % 2:
                continue
            gid = _grant(db, rid, vid, at + timedelta(minutes=1), "REVOKED" if i % 3 == 0 else "GRANTED")
            db.add_all([Distribution(distribution_id=new_id("distribution"), grant_id=gid, organization_id=ORG_CONSUMER,
                                     requested_by="t", file_count=k + 1, expires_at=at + timedelta(days=1),
                                     created_at=at + timedelta(minutes=1)) for k in range(2)])
            events += 3 + (1 if i % 3 == 0 else 0)
    return events


def test_usage_history_pages_in_time_order(ctx: Ctx) -> None:
    total = _history_rows(ctx, 7)
    (whole,) = _pages(ctx, "/v1/usage-history", ADMIN_A, 500)
    assert len(whole) == total
    assert [(x["at"], x["kind"]) for x in whole] == sorted((x["at"], x["kind"]) for x in whole)  # 시각·종류 순
    assert {x["kind"] for x in whole} == {"REQUESTED", "DECIDED", "GRANTED", "DOWNLOAD_ISSUED", "REVOKED"}
    first = whole[0]
    assert (first["kind"], first["detail"], first["consumer_organization"], first["purpose"]) == (
        "REQUESTED", "합성 신청 01:00:00", "이용기관 (시험)", "AI_TRAINING")
    details = {x["kind"]: x["detail"] for x in whole}
    assert details["DECIDED"] == "PROVIDER APPROVE: 합성 승인"
    assert details["GRANTED"].startswith("~ 2026-12-") and details["REVOKED"] == "합성 철회"
    assert {x["detail"] for x in whole if x["kind"] == "DOWNLOAD_ISSUED"} == {"파일 1개", "파일 2개"}

    pages = _pages(ctx, "/v1/usage-history", ADMIN_A, 4)
    assert all(len(p) <= 4 for p in pages) and len(pages) == -(-total // 4)
    assert [x for p in pages for x in p] == whole  # 쪽을 이어 붙이면 한 번에 받은 것과 같다
    bad = ctx.client.get("/v1/usage-history?cursor=bm90LWEtY3Vyc29y", headers=ctx.as_user(ADMIN_A))
    assert bad.status_code == 400 and bad.json()["errors"][0]["pointer"] == "/query/cursor", bad.text


def _statements(ctx: Ctx) -> int:
    seen: list[str] = []

    def count(_conn: Any, _cur: Any, statement: str, *_a: Any) -> None:
        seen.append(statement)

    event.listen(ctx.c.engine, "before_cursor_execute", count)
    try:
        assert ctx.client.get("/v1/usage-history?limit=500", headers=ctx.as_user(ADMIN_A)).status_code == 200
    finally:
        event.remove(ctx.c.engine, "before_cursor_execute", count)
    return len(seen)


def test_usage_history_statements_do_not_grow_with_requests(ctx: Ctx) -> None:
    """전: 신청마다 결정·권한·다운로드를 따로 읽어 신청 수에 비례해 문이 늘었다."""
    _history_rows(ctx, 2)
    few = _statements(ctx)
    _history_rows(ctx, 20)
    assert _statements(ctx) - few <= 2
