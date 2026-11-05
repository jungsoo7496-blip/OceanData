"""09번 요구사항 7.2: 구간 클립 내보내기 — 작업 등록(202) → 작업 큐 → CLIP 파생 자산·계보·옮긴 촬영 시각·구간 기록.

합성 영상(ffmpeg testsrc2, 4초·15fps·키프레임 1개)만 쓴다. 클립 첫 프레임이 원 영상 1.0초 프레임과 같고 0초(키프레임)·
1.2초 프레임과는 다른지, 길이가 구간과 같은지로 프레임 단위 정확도를 확인한다.
"""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app.db.models import Asset, AssetLineage
from tests.acceptance.test_t09_deid_review import gray_frame, original, video
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_pipeline import drain

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg가 필요합니다")]
ADMIN_A, MEMBER_A, ADMIN_B, VIEWER, OP = 101, 102, 103, 105, 106


def _job(ctx, job_id: str) -> tuple:  # type: ignore[no-untyped-def]
    with ctx.c.engine.connect() as conn:
        return tuple(conn.execute(text("SELECT status, attempts, last_error_code FROM jobs WHERE job_id = :j"),
                                  {"j": job_id}).one())


def _source(ctx, data: bytes) -> str:  # type: ignore[no-untyped-def]
    """계약에 맞는 원 영상: 촬영 시각 근거(DEVICE)와 위치 정밀도를 채운다 (UNKNOWN이면 촬영 시각이 없어야 한다)."""
    aid = original(ctx, data)
    with ctx.c.sessionmaker.begin() as db:
        a = db.get(Asset, aid)
        a.timestamp_quality = {"source": "DEVICE", "verified": False, "est_offset_ms": None}
        a.location_precision = "GENERALIZED"
    return aid


def _diff(a: bytes, b: bytes) -> float:
    return sum(abs(x - y) for x, y in zip(a, b, strict=True)) / len(a)


def test_clip_export_creates_linked_clip_asset(ictx, tmp_path: Path) -> None:
    ctx = ictx
    data = video(tmp_path, "testsrc2=size=320x240:rate=15", "a.mp4")
    src = _source(ctx, data)
    path = f"/v1/assets/{src}/clips"
    body = {"start_s": 1.0, "end_s": 2.5, "reason": "접근 선박 구간 (합성)"}

    # 권한: 운영자·관리기관 열람자는 바이너리를 다루지 않고(403), 다른 기관에는 보이지 않는다(404)
    assert ctx.client.post(path, json=body, headers=ctx.as_user(OP)).status_code == 403
    assert ctx.client.post(path, json=body, headers=ctx.as_user(VIEWER)).status_code == 403
    assert ctx.client.post(path, json=body, headers=ctx.as_user(ADMIN_B)).status_code == 404
    # 구간 검사 (작업을 만들지 않는다)
    for bad, code in (({"start_s": 2.0, "end_s": 2.0}, "range"), ({"start_s": 1.0, "end_s": 4.5}, "beyond_duration"),
                      ({"start_s": -1, "end_s": 1}, None), ({"start_s": 0, "end_s": 1, "x": 1}, None)):
        r = ctx.client.post(path, json=bad, headers=ctx.as_user(MEMBER_A))
        check(r, "POST", "/v1/assets/{asset_id}/clips")
        assert r.status_code == 400, (bad, r.text)
        if code:
            assert code in [e["code"] for e in r.json()["errors"]]
    ctx.c.settings.__dict__["clip_max_s"] = 1.0
    try:
        r = ctx.client.post(path, json=body, headers=ctx.as_user(MEMBER_A))
        assert r.status_code == 400 and r.json()["errors"][0]["code"] == "max_length"
    finally:
        ctx.c.settings.__dict__["clip_max_s"] = 3600.0

    # 등록 → 202 QUEUED. 같은 구간을 다시 누르면 진행 중인 같은 작업 (감사는 한 번)
    r = ctx.client.post(path, json=body, headers=ctx.as_user(MEMBER_A))
    check(r, "POST", "/v1/assets/{asset_id}/clips")
    assert r.status_code == 202 and r.json()["status"] == "QUEUED", r.text
    job_id = r.json()["job_id"]
    again = ctx.client.post(path, json=body, headers=ctx.as_user(ADMIN_A))
    assert again.status_code == 202 and again.json()["job_id"] == job_id
    assert ctx.audit_actions().count("CLIP_EXPORT_REQUESTED") == 1
    r = ctx.client.get(path, headers=ctx.as_user(MEMBER_A))
    check(r, "GET", "/v1/assets/{asset_id}/clips")
    assert [(x["job_id"], x["status"]) for x in r.json()["items"]] == [(job_id, "QUEUED")]
    assert ctx.client.get(path, headers=ctx.as_user(OP)).status_code == 403

    # 작업 큐 → DONE, 결과는 원 영상의 파생 자산(CLIP)으로 계보에 연결
    drain(ctx)
    assert _job(ctx, job_id)[0] == "DONE"
    r = ctx.client.get(path, headers=ctx.as_user(MEMBER_A))
    check(r, "GET", "/v1/assets/{asset_id}/clips")
    item = r.json()["items"][0]
    assert item["status"] == "DONE" and item["start_s"] == 1.0 and item["end_s"] == 2.5
    clip = item["clip_asset_id"]
    lineage = ctx.client.get(f"/v1/assets/{src}/lineage", headers=ctx.as_user(MEMBER_A)).json()
    assert {"asset_id": clip, "relation": "CLIP"}.items() <= next(
        d for d in lineage["descendants"] if d["asset_id"] == clip).items()
    r = ctx.client.get(f"/v1/assets/{clip}", headers=ctx.as_user(MEMBER_A))
    check(r, "GET", "/v1/assets/{asset_id}")
    a = r.json()
    assert (a["variant"], a["data_type"], a["sharing_scope"], a["ingest_status"]) == (
        "CLIP", "VIDEO", "PRIVATE", "READY_PRIVATE")
    assert a["provenance"]["clip_range_s"] == [1.0, 2.5] and a["provenance"]["job_id"] == job_id
    assert a["provenance"]["transform"] == "CLIP" and a["provenance"]["parent_asset_ids"] == [src]
    assert abs(a["video_ext"]["duration_s"] - 1.5) < 0.15
    with ctx.c.sessionmaker() as db:
        parent, child = db.get(Asset, src), db.get(Asset, clip)
        assert child.capture_start_at == parent.capture_start_at + timedelta(seconds=1)
        assert child.capture_end_at == parent.capture_start_at + timedelta(seconds=2.5)
        assert db.execute(select(AssetLineage.relation).where(AssetLineage.child_asset_id == clip)).scalar_one() \
            == "CLIP"
        key = child.storage_key
    # 프레임 단위 정확도: 클립 첫 프레임 ≈ 원 영상 1.0초 프레임, 0초(유일한 키프레임)·1.2초와는 다르다
    clip_bytes = ctx.c.storage.get_object(ctx.c.settings.bucket_proc, key)
    first = gray_frame(clip_bytes, 0, tmp_path)
    same = _diff(first, gray_frame(data, 1.0, tmp_path))
    for other in (0, 1.2):
        assert same * 3 < _diff(first, gray_frame(data, other, tmp_path)), (same, other)

    # 같은 구간을 다시 요청하면 새 작업이지만 결과는 같은 자산 (같은 체크섬 → create_derived가 기존 자산을 돌려준다)
    r = ctx.client.post(path, json=body, headers=ctx.as_user(MEMBER_A))
    assert r.status_code == 202 and r.json()["job_id"] != job_id
    drain(ctx)
    items = ctx.client.get(path, headers=ctx.as_user(MEMBER_A)).json()["items"]
    assert [x["status"] for x in items] == ["DONE", "DONE"] and {x["clip_asset_id"] for x in items} == {clip}


def test_clip_export_failures_are_not_retried(ictx, tmp_path: Path) -> None:
    ctx = ictx
    data = video(tmp_path, "testsrc2=size=320x240:rate=15", "b.mp4")
    gone = original(ctx, data)
    broken = original(ctx, b"not a video (synthetic)" * 64)  # 길이 정보는 있지만 디코딩할 수 없는 바이너리
    unknown = original(ctx, video(tmp_path, "testsrc2=size=320x240:rate=15,hue=s=0", "c.mp4"))
    with ctx.c.sessionmaker.begin() as db:
        db.get(Asset, unknown).ext = {}
    r = ctx.client.post(f"/v1/assets/{unknown}/clips", json={"start_s": 0, "end_s": 1}, headers=ctx.as_user(MEMBER_A))
    check(r, "POST", "/v1/assets/{asset_id}/clips")
    assert r.status_code == 409  # 길이를 모르면 구간을 검사할 수 없다

    j_gone = ctx.client.post(f"/v1/assets/{gone}/clips", json={"start_s": 0, "end_s": 1},
                             headers=ctx.as_user(MEMBER_A)).json()["job_id"]
    j_broken = ctx.client.post(f"/v1/assets/{broken}/clips", json={"start_s": 0, "end_s": 1},
                               headers=ctx.as_user(MEMBER_A)).json()["job_id"]
    with ctx.c.sessionmaker.begin() as db:  # 요청 뒤 격리됨
        db.get(Asset, gone).ingest_status = "QUARANTINED"
    drain(ctx)
    # 다시 해도 같은 실패는 재시도하지 않고 바로 FAILED (시도 1번)
    assert _job(ctx, j_gone) == ("FAILED", 1, "SOURCE_UNAVAILABLE")
    assert _job(ctx, j_broken) == ("FAILED", 1, "CLIP_ENCODE_FAILED")
    r = ctx.client.get(f"/v1/assets/{broken}/clips", headers=ctx.as_user(MEMBER_A))
    check(r, "GET", "/v1/assets/{asset_id}/clips")
    assert [(x["status"], x.get("error_code"), x.get("clip_asset_id")) for x in r.json()["items"]] == [
        ("FAILED", "CLIP_ENCODE_FAILED", None)]
    r = ctx.client.post(f"/v1/assets/{gone}/clips", json={"start_s": 0, "end_s": 1}, headers=ctx.as_user(MEMBER_A))
    assert r.status_code == 409  # 격리된 영상은 자를 수 없다
    with ctx.c.sessionmaker() as db:
        assert db.execute(select(AssetLineage).where(AssetLineage.relation == "CLIP")).first() is None
