"""T09 비식별 검수·T16-a CPU 동작 (docs/ACCEPTANCE.md), 14번 태깅·장면 검색.

합성 영상(ffmpeg testsrc2·검은 화면)과 모의 탐지기(asset.ext의 합성 영역)로 흐름을 검사한다. 실제 탐지 모델은
Model_Registry 라이선스 확인 후 붙인다 (14번 design.md).
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import Asset, AssetLineage, DeidReview, Event
from app.seed import DEV_A_CAMERA, ORG_A, PRJ_A, SES_A
from app.services import deid, tagging
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_catalog import _asset

pytestmark = [pytest.mark.integration, pytest.mark.acceptance,
              pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg가 필요합니다")]
REVIEWER, OP, ADMIN = 108, 106, 101
FACE1 = {"cls": "FACE", "t0": 0.5, "t1": 1.4, "x": 40, "y": 40, "w": 64, "h": 64}
FACE2 = {"cls": "FACE", "t0": 1.6, "t1": 2.6, "x": 44, "y": 42, "w": 64, "h": 64}  # 0.2초 끊김 → 추적으로 이음


def video(tmp: Path, src: str, name: str) -> bytes:
    out = tmp / name
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", src, "-t", "4",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)], check=True)
    return out.read_bytes()


def gray_frame(data: bytes, t: float, tmp: Path) -> bytes:
    p = tmp / f"g{hashlib.sha256(data).hexdigest()[:8]}.mp4"
    p.write_bytes(data)
    return subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(t), "-i", str(p),
                           "-frames:v", "1", "-vf", "format=gray", "-f", "rawvideo", "-"],
                          capture_output=True, check=True).stdout


def original(ctx, data: bytes, **ext) -> str:
    with ctx.c.sessionmaker.begin() as db:
        a = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
        a.data_role, a.sensitivity_class, a.share_status, a.legal_basis_ref = "BYPRODUCT", "INTERNAL", "PROCESSING", "LB"
        a.capture_start_at = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)
        a.storage_zone, a.storage_key = "RAW", f"{ORG_A}/raw/{a.asset_id}.mp4"
        a.byte_size, a.checksum_sha256 = len(data), hashlib.sha256(data).hexdigest()
        a.ext = {"video_ext": {"resolution": "320x240", "fps": 15, "duration_s": 4}, **ext}
        db.add(a)
        aid = a.asset_id
    ctx.c.storage.put_object(ctx.c.settings.bucket_raw, f"{ORG_A}/raw/{aid}.mp4", data, "video/mp4")
    return aid


def review_for(ctx, orig: str) -> dict:
    r = ctx.client.get("/v1/deid-reviews", params={"status": "ASSIGNED"}, headers=ctx.as_user(REVIEWER))
    return next(x for x in r.json()["items"] if x["original_asset_id"] == orig)


def test_t09_deid_review_flow(ictx, tmp_path: Path) -> None:
    ctx = ictx
    data = video(tmp_path, "testsrc2=size=320x240:rate=15", "a.mp4")
    o1 = original(ctx, data, synthetic_regions=[FACE1, FACE2])
    o2 = original(ctx, video(tmp_path, "testsrc2=size=320x240:rate=15,hue=s=0", "b.mp4"),
                  synthetic_regions=[{**FACE1, "cls": "LICENSE_PLATE"}])

    # 요구사항 1: 공유 심사 PROCESSING 원본만 처리
    with ctx.c.sessionmaker.begin() as db:
        db.get(Asset, o2).share_status = "NONE"
    assert deid.deidentify(ctx.c, o2, None, {}) == "SKIPPED"
    with ctx.c.sessionmaker.begin() as db:
        db.get(Asset, o2).share_status = "PROCESSING"

    # 요구사항 2·3: 가린 새 DEID 파생본, 원본 불변, 처리 이력, QUALITY_REVIEW·검수자 배정
    assert deid.deidentify(ctx.c, o1, None, {"method": "FILL"}) == "OK"
    with ctx.c.sessionmaker() as db:
        orig = db.get(Asset, o1)
        child_id = db.execute(select(AssetLineage.child_asset_id).where(
            AssetLineage.parent_asset_id == o1, AssetLineage.relation == "DEID")).scalar_one()
        child = db.get(Asset, child_id)
        assert (orig.share_status, orig.checksum_sha256) == ("QUALITY_REVIEW", hashlib.sha256(data).hexdigest())
        assert (child.variant, child.storage_zone, child.review_result) == ("DEID", "PROCESSED", None)
        rec = child.ext["deid"]
        assert rec["model"]["name"] == "synthetic-regions" and rec["detections"] == {"FACE": 1}  # 추적으로 합침
        assert rec["params"]["method"] == "FILL" and child.provenance["deid"]["processed_at"]
    out = ctx.c.storage.get_object(ctx.c.settings.bucket_proc, child.storage_key)
    before, after = gray_frame(data, 1.5, tmp_path), gray_frame(out, 1.5, tmp_path)  # 끊긴 구간도 가려짐

    def mean(buf: bytes, x0: int, y0: int) -> float:
        return sum(buf[(y0 + j) * 320 + x0 + i] for j in range(20) for i in range(20)) / 400

    def diff(x0: int, y0: int) -> float:
        return sum(abs(before[(y0 + j) * 320 + x0 + i] - after[(y0 + j) * 320 + x0 + i])
                   for j in range(20) for i in range(20)) / 400

    assert mean(after, 60, 60) < 25 < mean(before, 60, 60) and diff(220, 160) < 6  # 영역 안은 가리고 밖은 그대로

    # T09-a: 검수 전 비식별본은 공유 불가
    r = ctx.client.post(f"/v1/assets/{child_id}/share-requests", headers=ctx.as_user(ADMIN),
                        json={"target_scope": "SANDBOX_PARTNERS", "reason": "시험"})
    assert r.status_code == 422 and "review_result=PASSED" in r.text

    # 검수: 배정된 것만, 원본 대비 프레임(감사), 통과 → 파생본에만 PASSED·PSEUDONYMIZED
    r = ctx.client.get("/v1/deid-reviews", headers=ctx.as_user(REVIEWER))
    check(r, "GET", "/v1/deid-reviews")
    rv1 = review_for(ctx, o1)
    assert rv1["samples"] and rv1["processing"]["model"]["name"] == "synthetic-regions"
    assert ctx.client.get("/v1/deid-reviews", headers=ctx.as_user(ADMIN)).status_code == 403
    r = ctx.client.get(f"/v1/deid-reviews/{rv1['review_id']}/frame", params={"t": 1.0, "which": "ORIGINAL"},
                       headers=ctx.as_user(REVIEWER))
    assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"
    assert "DEID_REVIEW_FRAME_VIEWED" in ctx.audit_actions()
    r = ctx.client.post(f"/v1/deid-reviews/{rv1['review_id']}/decision", json={"result": "PASS"},
                        headers=ctx.as_user(REVIEWER))
    check(r, "POST", "/v1/deid-reviews/{review_id}/decision")
    with ctx.c.sessionmaker() as db:
        c1 = db.get(Asset, child_id)
        assert (c1.review_result, c1.privacy_status) == ("PASSED", "PSEUDONYMIZED")
        assert db.get(Asset, o1).privacy_status != "PSEUDONYMIZED"  # 원본에는 기록하지 않는다
    assert ctx.client.post(f"/v1/deid-reviews/{rv1['review_id']}/decision", json={"result": "PASS"},
                           headers=ctx.as_user(REVIEWER)).status_code == 409

    # T09-b: 다른 영상에서 미탐 → 수동 가림 다시 렌더링 + 같은 회차 비식별 결과 재검수
    assert deid.deidentify(ctx.c, o2, None, {}) == "OK"
    rv2 = review_for(ctx, o2)
    r = ctx.client.post(f"/v1/deid-reviews/{rv2['review_id']}/decision", headers=ctx.as_user(REVIEWER), json={
        "result": "MISSED", "missed": [{"cls": "FACE", "t": 3.0}],
        "manual_regions": [{"t0": 2.8, "t1": 3.4, "x": 200, "y": 100, "w": 50, "h": 50}], "note": "오른쪽 얼굴 미탐"})
    assert r.json()["status"] == "MISSED" and len(r.json()["recheck_review_ids"]) == 1
    with ctx.c.sessionmaker() as db:
        assert db.get(Asset, child_id).review_result is None  # 재검수 전 공유 조건에서 빠짐
        assert db.get(Asset, o2).share_status == "PROCESSING"  # REPROCESS → PROCESSING(재처리 등록)
        recheck = db.get(DeidReview, r.json()["recheck_review_ids"][0])
        assert recheck.recheck and recheck.deid_asset_id == child_id
    job = ctx.c.queue.claim(["deidentify"], "t", ("CPU",))
    assert job is not None and job.payload["manual_regions"][0]["x"] == 200
    assert deid.deidentify(ctx.c, o2, job.job_id, job.payload) == "OK"
    with ctx.c.sessionmaker() as db:
        kids = [db.get(Asset, k) for k in db.execute(select(AssetLineage.child_asset_id).where(
            AssetLineage.parent_asset_id == o2, AssetLineage.relation == "DEID")).scalars()]
        assert sorted(k.review_result or "-" for k in kids) == ["-", "FAILED"]
        assert next(k for k in kids if k.review_result is None).ext["deid"]["manual_regions"] == 1

    # T09-c: 미탐률·재처리 비율
    r = ctx.client.get("/v1/deid-quality", headers=ctx.as_user(OP))
    check(r, "GET", "/v1/deid-quality")
    q = {x["class"]: x for x in r.json()["classes"]}
    assert (q["FACE"]["detected"], q["FACE"]["missed"]) == (1, 1) and q["FACE"]["miss_rate"] == 0.5
    assert r.json()["reprocess_ratio"] == 0.5 and "보증하지 않습니다" in r.json()["note"]

    # 요구사항 5.3: ANONYMIZED_VERIFIED는 파생본·legal_basis_ref·운영자
    body = {"privacy_status": "ANONYMIZED_VERIFIED", "basis_ref": "점검-1", "reason": "확인"}
    assert ctx.client.patch(f"/v1/assets/{o1}/classification", json=body, headers=ctx.as_user(OP)).status_code == 422
    assert ctx.client.patch(f"/v1/assets/{child_id}/classification", json=body,
                            headers=ctx.as_user(OP)).status_code == 422  # 아직 legal_basis_ref 없음 (승인 전)


def test_tagging_events_and_scene_search(ictx, tmp_path: Path) -> None:
    ctx = ictx
    day = original(ctx, video(tmp_path, "testsrc2=size=320x240:rate=15", "d.mp4"), synthetic_objects=[
        {"tag": "VESSEL_SMALL", "t0": 0, "t1": 2.5, "confidence": 0.9},
        {"tag": "PERSON", "t0": 1, "t1": 2, "confidence": 0.95}])
    night = original(ctx, video(tmp_path, "color=c=0x101010:size=320x240:rate=15", "n.mp4"), synthetic_objects=[
        {"tag": "FLOATING_OBJECT", "t0": 0, "t1": 4, "confidence": 0.85}])
    with ctx.c.sessionmaker.begin() as db:
        for x in (day, night):
            db.get(Asset, x).session_id = SES_A
    assert tagging.tag_scenes(ctx.c, day) == "OK" and tagging.tag_scenes(ctx.c, night) == "OK"
    r = ctx.client.get(f"/v1/assets/{night}/tags", headers=ctx.as_user(102))
    check(r, "GET", "/v1/assets/{asset_id}/tags")
    assert {"NIGHT", "FLOATING_OBJECT"} <= {t["tag"] for t in r.json()["tags"]}
    tags_day = {t["tag"] for t in ctx.client.get(f"/v1/assets/{day}/tags", headers=ctx.as_user(102)).json()["tags"]}
    assert {"DAY", "VESSEL_SMALL", "PERSON"} <= tags_day
    # 요구사항 6.4: 규칙에 맞는 탐지 → 모델 이벤트(미확인)
    with ctx.c.sessionmaker() as db:
        evs = list(db.execute(select(Event).where(Event.model_ref == tagging.MODEL)).scalars())
        assert {e.event_code for e in evs} == {"PERSON", "FLOATING_OBJECT"}
        assert all(e.source == "MODEL" and not e.human_verified for e in evs)
    # 요구사항 6.2: 사람 수정 이력
    r = ctx.client.patch(f"/v1/assets/{day}/tags", json={"add": ["BUOY"], "remove": ["PERSON"]},
                         headers=ctx.as_user(102))
    check(r, "PATCH", "/v1/assets/{asset_id}/tags")
    assert [e["action"] for e in r.json()["edits"]] == ["ADD", "REMOVE"]
    assert ctx.client.patch(f"/v1/assets/{day}/tags", json={"add": ["UFO"]},
                            headers=ctx.as_user(102)).status_code == 400

    # 요구사항 7: 문장 검색 (권한 범위 안)
    r = ctx.client.get("/v1/scenes/search", params={"q": "야간 부유물"}, headers=ctx.as_user(102))
    check(r, "GET", "/v1/scenes/search")
    assert r.json()["query_tags"] == ["FLOATING_OBJECT", "NIGHT"] and r.json()["items"][0]["asset_id"] == night
    r = ctx.client.get("/v1/scenes/search", params={"q": "소형 선박이 지나가는 낮 장면"}, headers=ctx.as_user(102))
    assert r.json()["items"][0]["asset_id"] == day and r.json()["items"][0]["score"] > 0.8
    assert ctx.client.get("/v1/scenes/search", params={"q": "zzz"}, headers=ctx.as_user(102)).status_code == 400
    assert ctx.client.get("/v1/scenes/search", params={"q": "야간"}, headers=ctx.as_user(103)).json()["items"] == []
