"""구간 클립 작업 목록의 커서 페이지 (09번 요구사항 7.2, api-conventions.md 목록 규칙, 계약 점검 S29, ADR-0077).

전에는 최근 100건에서 말없이 잘려 그보다 오래된 작업(상태·실패 원인·결과 자산)을 볼 수 없었다.
기본 페이지 크기는 그대로 100건이고, next_cursor로 다음 페이지를 받는다. 순서는 요청 시각 역순, 같으면 작업 ID 역순.
"""

from __future__ import annotations

import base64
import json
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.db.models import AssetLineage
from app.seed import DEV_A_CAMERA, ORG_A, PRJ_A, fid
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_catalog import T0, _asset

pytestmark = pytest.mark.integration
MEMBER_A, ADMIN_B = 102, 103
CLIPS = "/v1/assets/{asset_id}/clips"


def _video(ctx: Ctx) -> str:
    with ctx.c.sessionmaker.begin() as db:
        a = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
        a.location = None
        a.storage_zone, a.storage_key = "RAW", f"{ORG_A}/raw/{a.asset_id}"
        a.ext = {"video_ext": {"duration_s": 600.0}}
        db.add(a)
        return a.asset_id


def _done_jobs(ctx: Ctx, asset_id: str, n: int) -> list[str]:
    """끝난 클립 작업 n개. 요청 시각은 세 개씩 같게 해 작업 ID로 순서가 갈리는 경우도 만든다.

    돌려주는 목록은 계약이 정한 순서(요청 시각 역순, 같으면 작업 ID 역순)를 DB가 정렬한 것이다."""
    ids = [ctx.c.queue.enqueue("clip_export", {"asset_id": asset_id, "start_s": float(k), "end_s": k + 0.5,
                                               "requested_by": fid("usr", MEMBER_A)}, organization_id=ORG_A)
           for k in range(n)]
    with ctx.c.engine.begin() as conn:
        for k, job_id in enumerate(ids):
            conn.execute(text("UPDATE jobs SET status = 'DONE', created_at = :at WHERE job_id = :j"),
                         {"at": T0 + timedelta(seconds=k // 3), "j": job_id})
        return list(conn.execute(text("SELECT job_id FROM jobs WHERE job_type = 'clip_export' AND asset_id = :a "
                                      "ORDER BY created_at DESC, job_id DESC"), {"a": asset_id}).scalars())


def _clip_result(ctx: Ctx, parent: str, job_id: str, start_s: float) -> str:
    with ctx.c.sessionmaker.begin() as db:
        clip = _asset(ORG_A, PRJ_A, DEV_A_CAMERA, variant="CLIP")
        clip.location = None
        clip.provenance = {"ingest_channel": "AGENT", "job_id": job_id, "clip_range_s": [start_s, start_s + 0.5],
                           "parent_asset_ids": [parent]}
        db.add(clip)
        db.flush()
        db.add(AssetLineage(child_asset_id=clip.asset_id, parent_asset_id=parent, relation="CLIP",
                            tool_version="ffmpeg", created_at=T0))
        return clip.asset_id


def _get(ctx: Ctx, asset_id: str, query: str = "") -> dict:
    r = ctx.client.get(CLIPS.format(asset_id=asset_id) + query, headers=ctx.as_user(MEMBER_A))
    assert r.status_code == 200, r.text
    check(r, "GET", CLIPS)
    body: dict = r.json()
    return body


def test_clip_list_pages_through_every_job(ctx: Ctx) -> None:
    aid = _video(ctx)
    expected = _done_jobs(ctx, aid, 105)
    oldest_clip = _clip_result(ctx, aid, expected[-1], 900.0)  # 가장 오래된 작업의 결과 자산 (작업 ID로 찾는다)

    first = _get(ctx, aid)  # 조건 없이: 지금처럼 최근 100건, 그리고 다음 페이지 커서
    assert [x["job_id"] for x in first["items"]] == expected[:100]
    assert "next_cursor" in first and isinstance(first["next_cursor"], str), \
        "100건을 넘는 클립 작업 목록에 다음 페이지 커서가 없습니다"
    rest = _get(ctx, aid, f"?cursor={first['next_cursor']}")
    assert [x["job_id"] for x in rest["items"]] == expected[100:]
    assert rest["next_cursor"] is None
    last = rest["items"][-1]
    assert (last["job_id"], last["status"], last.get("clip_asset_id")) == (expected[-1], "DONE", oldest_clip)

    seen: list[str] = []  # 작은 페이지로 끝까지 — 같은 시각의 작업도 빠지거나 겹치지 않는다
    query = "?limit=7"
    while True:
        page = _get(ctx, aid, query)
        assert 1 <= len(page["items"]) <= 7
        seen += [x["job_id"] for x in page["items"]]
        if page["next_cursor"] is None:
            break
        query = f"?limit=7&cursor={page['next_cursor']}"
    assert seen == expected
    assert [x["job_id"] for x in _get(ctx, aid, "?limit=500")["items"]] == expected


def _bad(ctx: Ctx, asset_id: str, query: str) -> list[str]:
    r = ctx.client.get(CLIPS.format(asset_id=asset_id) + query, headers=ctx.as_user(MEMBER_A))
    assert r.status_code == 400, (query, r.status_code, r.text)
    check(r, "GET", CLIPS)
    assert r.json()["code"] == "VALIDATION_ERROR"
    return [e["pointer"] for e in r.json()["errors"]]


def _cursor(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def test_clip_list_rejects_bad_paging_input(ctx: Ctx) -> None:
    aid = _video(ctx)
    for query in ("?limit=0", "?limit=501"):
        assert _bad(ctx, aid, query) == ["/query/limit"]
    job_id = _done_jobs(ctx, aid, 1)[0]
    for cursor in ("a.b", base64.urlsafe_b64encode(b"not json").decode().rstrip("="), _cursor(["x", job_id]),
                   _cursor([T0.isoformat(), "ast_0000000000000000000000ZZZZ"]), _cursor({"at": T0.isoformat()}),
                   _cursor(["2026-11-01T00:00:00", job_id])):  # 시간대 없는 시각도 거부
        assert _bad(ctx, aid, f"?cursor={cursor}") == ["/query/cursor"], cursor
    other_org = ctx.client.get(CLIPS.format(asset_id=aid) + "?limit=1", headers=ctx.as_user(ADMIN_B))
    assert other_org.status_code == 404, other_org.text  # 다른 기관은 페이지 조건과 상관없이 404


def test_clip_cursor_outside_utc_range_is_400_and_later_requests_still_work(ctx: Ctx) -> None:
    """시간대가 있어 읽히지만 UTC로 바꾸면 1~9999년 밖으로 나가는 커서 시각.

    전에는 DB 드라이버가 매개변수를 UTC로 바꾸다 OverflowError로 500이 났다.
    그 연결이 망가진 채 풀로 돌아가 뒤따르는 정상 요청까지 500이 됐다(검토 지적)."""
    aid = _video(ctx)
    job_id = _done_jobs(ctx, aid, 1)[0]
    edges = ("0001-01-01T00:00:00+14:00", "0001-01-01T00:00:00+23:59",
             "9999-12-31T23:59:59.999999-14:00", "9999-12-31T23:59:59-23:59")
    got = []
    for at in edges:
        r = ctx.client.get(CLIPS.format(asset_id=aid) + f"?cursor={_cursor([at, job_id])}",
                           headers=ctx.as_user(MEMBER_A))
        pointers = [e.get("pointer") for e in r.json().get("errors", [])] if r.status_code == 400 else r.text[:80]
        got.append((at, r.status_code, pointers))
    plain = [ctx.client.get(path.format(asset_id=aid), headers=ctx.as_user(MEMBER_A)).status_code
             for path in (CLIPS, "/v1/assets/{asset_id}") * 4]
    assert got == [(at, 400, ["/query/cursor"]) for at in edges], "UTC 범위 밖 커서 시각이 400이 아닙니다"
    assert plain == [200] * 8, "범위 밖 커서 뒤의 정상 요청이 실패합니다 (풀의 DB 연결이 망가짐)"
    assert _bad(ctx, aid, f"?cursor={_cursor([edges[0], job_id])}") == ["/query/cursor"]  # 400도 계약 형식
    edge_ok = _get(ctx, aid, f"?cursor={_cursor(['0001-01-01T00:00:00+00:00', job_id])}")  # 범위 안 경계는 그대로 200
    assert edge_ok["items"] == [] and edge_ok["next_cursor"] is None
