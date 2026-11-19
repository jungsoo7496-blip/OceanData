"""비식별 검수 목록의 페이지 (통합 검토 ASSETS-5, api-conventions.md 목록 규칙, ADR-0089).

GET /v1/deid-reviews에는 limit·cursor가 없어 운영자에게 플랫폼의 모든 검수를 한 번에 주고, 행마다 비식별본을 따로
읽었다. 이제 limit(기본 50·최대 500)건씩 만든 시각·검수 ID 순으로 주고, 다음 쪽은 next_cursor로 이어 본다.
시험 데이터는 합성 값이다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event

from app.api.pagination import encode_cursor
from app.db.models import AssetLineage, DeidReview
from app.domain.ids import new_id
from app.seed import DEV_A_CAMERA, ORG_A, PRJ_A, SES_A, fid
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_catalog import _asset

pytestmark = pytest.mark.integration
OP, REVIEWER = 106, 108
PATH = "/v1/deid-reviews"
T0 = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)


def _reviews(ctx: Ctx, offsets: list[int], reviewer: str = fid("usr", REVIEWER),
             status: str = "ASSIGNED") -> list[str]:
    """한 원본·비식별본 쌍에 대한 검수들. offsets[i]분 뒤에 만든 것으로 둔다 (ID 순과 만든 순이 다르게)."""
    with ctx.c.sessionmaker.begin() as db:
        o, ch = _asset(ORG_A, PRJ_A, DEV_A_CAMERA), _asset(ORG_A, PRJ_A, DEV_A_CAMERA, variant="DEID")
        db.add_all([o, ch])
        db.flush()
        db.add(AssetLineage(child_asset_id=ch.asset_id, parent_asset_id=o.asset_id, relation="DEID",
                            tool_version="t", created_at=T0))
        rows = [DeidReview(review_id=new_id("review"), organization_id=ORG_A, session_id=SES_A,
                           original_asset_id=o.asset_id, deid_asset_id=ch.asset_id, reviewer_id=reviewer,
                           risk_level="LOW", samples=[0.5], recheck=False, status=status, missed=[],
                           manual_regions=[], created_at=T0 + timedelta(minutes=m)) for m in offsets]
        db.add_all(rows)
        return [r.review_id for r in rows]


def _get(ctx: Ctx, user: int, **params: Any) -> Any:
    r = ctx.client.get(PATH, params=params, headers=ctx.as_user(user))
    check(r, "GET", PATH)
    return r


def _ordered(ctx: Ctx, ids: list[str]) -> list[str]:
    """만든 시각, 같으면 검수 ID 순 (목록의 순서)."""
    with ctx.c.sessionmaker() as db:
        rows = [db.get(DeidReview, x) for x in ids]
    return [r.review_id for r in sorted(rows, key=lambda r: (r.created_at, r.review_id))]


def test_operator_list_is_paged_in_creation_order(ctx: Ctx) -> None:
    ids = _reviews(ctx, [3, 1, 2, 1, 0])  # 같은 시각(1분)이 둘
    want = _ordered(ctx, ids)
    seen, cursor, pages = [], None, 0
    while True:
        r = _get(ctx, OP, limit=2, **({"cursor": cursor} if cursor else {}))
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["items"]) <= 2
        seen += [x["review_id"] for x in body["items"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert pages < 5, "다음 쪽이 끝나지 않는다"
    assert (seen, pages) == (want, 3)


def test_default_page_is_50_with_a_next_cursor(ctx: Ctx) -> None:
    ids = _reviews(ctx, list(range(51)))
    r = _get(ctx, OP)
    assert r.status_code == 200, r.text
    first = r.json()
    assert len(first["items"]) == 50 and first["next_cursor"], "기본 페이지 크기(50)로 자르지 않았다"
    rest = _get(ctx, OP, cursor=first["next_cursor"]).json()
    assert [x["review_id"] for x in first["items"] + rest["items"]] == _ordered(ctx, ids)
    assert rest["next_cursor"] is None


def test_reviewer_pages_only_assigned_reviews_and_status_filter_keeps_the_cursor(ctx: Ctx) -> None:
    mine = _reviews(ctx, [0, 2, 4])
    others = _reviews(ctx, [1, 3], reviewer=fid("usr", 199))  # 다른 검수자 (합성)
    passed = _reviews(ctx, [5], status="PASSED")
    first = _get(ctx, REVIEWER, status="ASSIGNED", limit=2).json()
    assert [x["review_id"] for x in first["items"]] == _ordered(ctx, mine)[:2]
    rest = _get(ctx, REVIEWER, status="ASSIGNED", limit=2, cursor=first["next_cursor"]).json()
    assert ([x["review_id"] for x in rest["items"]], rest["next_cursor"]) == (_ordered(ctx, mine)[2:], None)
    everything = _get(ctx, REVIEWER, limit=10).json()
    assert [x["review_id"] for x in everything["items"]] == _ordered(ctx, mine + passed)
    # 볼 수 없는 검수 ID로 만든 커서는 형식이 틀린 커서와 같다 (다른 검수자의 검수가 있는지 알려 주지 않는다)
    hidden = _get(ctx, REVIEWER, cursor=encode_cursor(others[0]))
    assert (hidden.status_code, hidden.json()["errors"][0]["pointer"]) == (400, "/query/cursor"), hidden.text


@pytest.mark.parametrize(("params", "pointer"), [
    ({"limit": 0}, "/query/limit"),
    ({"limit": 501}, "/query/limit"),
    ({"cursor": "bm90LWEtcmV2aWV3"}, "/query/cursor"),  # 검수 ID가 아닌 값
    ({"cursor": encode_cursor(new_id("review"))}, "/query/cursor"),  # 없는 검수
])
def test_bad_limit_or_cursor_is_400(ctx: Ctx, params: dict[str, Any], pointer: str) -> None:
    r = _get(ctx, OP, **params)
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "VALIDATION_ERROR"
    assert [e["pointer"] for e in r.json()["errors"]] == [pointer]


def test_page_reads_derived_assets_in_one_statement(ctx: Ctx) -> None:
    """행마다 비식별본을 따로 읽지 않는다 — 쪽의 검수가 많아도 문 수가 늘지 않는다."""
    few, many = _reviews(ctx, [0, 1], status="PASSED"), _reviews(ctx, list(range(2, 22)))
    with ctx.c.sessionmaker.begin() as db:  # 검수마다 다른 비식별본
        for rid in few + many:
            ch = _asset(ORG_A, PRJ_A, DEV_A_CAMERA, variant="DEID")
            db.add(ch)
            db.flush()
            rv = db.get(DeidReview, rid)
            assert rv is not None
            rv.deid_asset_id = ch.asset_id

    def count(status: str) -> int:
        seen: list[str] = []

        def on(_conn: Any, _cur: Any, statement: str, *_a: Any) -> None:
            seen.append(statement)

        event.listen(ctx.c.engine, "before_cursor_execute", on)
        try:
            r = _get(ctx, OP, status=status, limit=50)
        finally:
            event.remove(ctx.c.engine, "before_cursor_execute", on)
        assert r.status_code == 200, r.text
        assert len(r.json()["items"]) == len(few if status == "PASSED" else many)
        return len(seen)

    assert count("ASSIGNED") - count("PASSED") <= 1, "검수마다 비식별본을 따로 읽는다"
