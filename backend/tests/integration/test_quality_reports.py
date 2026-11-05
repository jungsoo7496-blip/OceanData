"""03번 요구사항 10.1 품질 집계 `GET /v1/quality-reports` — 기관 범위·원본만·회차·기간 [from, to)·상위 사유.

계약에는 G1부터 있었으나 라우트가 없어 404만 돌려주던 작업이다 (계약-라우트 일치 시험이 이후 같은 누락을 막는다). 값은 모두 합성이다.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.db.models import Asset
from app.domain.ids import new_id
from app.seed import DEV_A_CAMERA, DEV_B_CAMERA, ORG_A, ORG_B, PRJ_A, PRJ_B, SES_A, SES_B
from tests.conftest import in_future
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_catalog import T0, _asset

pytestmark = pytest.mark.integration
PATH = "/v1/quality-reports"
ADMIN_A, MEMBER_A, ADMIN_B, VIEWER, OP, REVIEWER, CONSUMER = 101, 102, 103, 105, 106, 108, 109


def _put(db, org: str, prj: str, dev: str, status: str, reasons: list[str], *, session: str | None = SES_A,  # type: ignore[no-untyped-def]
         variant: str = "ORIGINAL", day: int = 0, deleted: bool = False) -> Asset:
    a = _asset(org, prj, dev, variant=variant, session=session)
    a.quality_status, a.quality_reasons, a.ingested_at = status, reasons, T0 + timedelta(days=day)
    if deleted:
        a.deleted_at = T0 + timedelta(days=3)
    db.add(a)
    return a


@pytest.fixture()
def seeded(ctx):  # type: ignore[no-untyped-def]
    with ctx.c.sessionmaker.begin() as db:
        _put(db, ORG_A, PRJ_A, DEV_A_CAMERA, "PASSED", [])
        _put(db, ORG_A, PRJ_A, DEV_A_CAMERA, "WARNING", ["capture_time_unknown"])
        _put(db, ORG_A, PRJ_A, DEV_A_CAMERA, "WARNING", ["capture_time_unknown", "unknown_unit"], day=1)
        _put(db, ORG_A, PRJ_A, DEV_A_CAMERA, "FAILED", ["capture_time_unknown", "not_playable"], session=None, day=2)
        # 세지 않는 것: 파생본, 삭제 표시된 원본
        _put(db, ORG_A, PRJ_A, DEV_A_CAMERA, "WARNING", ["capture_time_unknown"], variant="PROXY")
        _put(db, ORG_A, PRJ_A, DEV_A_CAMERA, "FAILED", ["not_playable"], deleted=True)
        # 다른 기관
        _put(db, ORG_B, PRJ_B, DEV_B_CAMERA, "FAILED", ["not_playable"], session=SES_B)
    return ctx


def _get(ctx, who: int, **params: str):  # type: ignore[no-untyped-def]
    return ctx.client.get(PATH, params=params, headers=ctx.as_user(who))


def test_counts_are_own_org_originals_only_with_ranked_reasons(seeded) -> None:
    r = _get(seeded, MEMBER_A)
    check(r, "GET", PATH)
    body = r.json()
    assert body["counts_by_quality_status"] == {"PENDING": 0, "PASSED": 1, "WARNING": 2, "FAILED": 1}
    # 많은 순, 같으면 사유 이름순
    assert body["top_reasons"] == [{"reason": "capture_time_unknown", "count": 3},
                                   {"reason": "not_playable", "count": 1}, {"reason": "unknown_unit", "count": 1}]
    assert not {"session_id", "from", "to"} & set(body)

    op = _get(seeded, OP).json()  # 운영자는 전체 기관
    assert op["counts_by_quality_status"] == {"PENDING": 0, "PASSED": 1, "WARNING": 2, "FAILED": 2}
    assert op["top_reasons"][:2] == [{"reason": "capture_time_unknown", "count": 3},
                                     {"reason": "not_playable", "count": 2}]
    b = _get(seeded, ADMIN_B).json()
    assert b["counts_by_quality_status"]["FAILED"] == 1 and b["top_reasons"] == [{"reason": "not_playable", "count": 1}]


def test_session_and_period_filters(seeded) -> None:
    r = _get(seeded, MEMBER_A, session_id=SES_A)
    check(r, "GET", PATH)
    assert r.json()["session_id"] == SES_A
    assert r.json()["counts_by_quality_status"] == {"PENDING": 0, "PASSED": 1, "WARNING": 2, "FAILED": 0}
    assert r.json()["top_reasons"] == [{"reason": "capture_time_unknown", "count": 2},
                                       {"reason": "unknown_unit", "count": 1}]
    # 다른 기관 회차·없는 회차는 존재 여부를 드러내지 않는다
    assert _get(seeded, MEMBER_A, session_id=SES_B).status_code == 404
    assert _get(seeded, MEMBER_A, session_id=new_id("session")).status_code == 404
    assert _get(seeded, ADMIN_B, session_id=SES_B).json()["counts_by_quality_status"]["FAILED"] == 1

    # 기간은 ingested_at 기준 [from, to): 하루째 자산 하나만
    day1, day2 = "2026-11-02T00:00:00Z", "2026-11-03T00:00:00Z"
    r = _get(seeded, MEMBER_A, **{"from": day1, "to": day2})
    check(r, "GET", PATH)
    assert r.json()["counts_by_quality_status"] == {"PENDING": 0, "PASSED": 0, "WARNING": 1, "FAILED": 0}
    assert (r.json()["from"], r.json()["to"]) == (day1, day2)
    only_from = _get(seeded, MEMBER_A, **{"from": day2}).json()["counts_by_quality_status"]
    assert only_from == {"PENDING": 0, "PASSED": 0, "WARNING": 0, "FAILED": 1}
    for bad in ({"from": day2, "to": day1}, {"from": day1, "to": day1}):
        r = _get(seeded, MEMBER_A, **bad)
        check(r, "GET", PATH)
        assert r.status_code == 400 and r.json()["errors"][0]["pointer"] == "/query/to"
    assert _get(seeded, MEMBER_A, **{"from": "2026-11-02"}).status_code == 400


def test_role_and_api_key_scope(seeded) -> None:
    for who in (VIEWER, REVIEWER, CONSUMER):
        r = _get(seeded, who)
        check(r, "GET", PATH)
        assert r.status_code == 403, who
    assert seeded.client.get(PATH).status_code == 401

    def key_headers(scope: str) -> dict[str, str]:
        k = seeded.client.post("/v1/api-keys", headers=seeded.as_user(ADMIN_A, mfa=True),
                               json={"name": f"bi-{scope}", "scopes": [scope], "expires_at": in_future(days=1)}).json()
        tok = seeded.client.post("/v1/auth/token", data={"grant_type": "client_credentials", "client_id": k["client_id"],
                                                         "client_secret": k["client_secret"], "scope": scope})
        return {"Authorization": "Bearer " + tok.json()["access_token"]}

    r = seeded.client.get(PATH, headers=key_headers("status:read"))  # 기관 API 키도 자기 기관만
    assert r.status_code == 200 and r.json()["counts_by_quality_status"]["FAILED"] == 1
    assert seeded.client.get(PATH, headers=key_headers("upload:write")).status_code == 403
