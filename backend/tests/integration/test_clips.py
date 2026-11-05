"""09번 요구사항 7.2: 구간 클립 내보내기 — 작업 등록(202) → 작업 큐 → CLIP 파생 자산·계보·옮긴 촬영 시각·구간 기록.

합성 영상(ffmpeg testsrc2, 4초·15fps·키프레임 1개)만 쓴다. 클립 첫 프레임이 원 영상 1.0초 프레임과 같고 0초(키프레임)·
1.2초 프레임과는 다른지, 길이가 구간과 같은지로 프레임 단위 정확도를 확인한다.
ADR-0033 보완: 쿼터·기관별 대기 작업 한도, 작업 등록과 감사의 원자성, 원 영상 삭제와 겹칠 때(삭제 먼저·작업 먼저).
"""

from __future__ import annotations

import hashlib
import shutil
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text

from app.db.models import Asset, AssetLineage
from app.seed import DEV_B_CAMERA, ORG_A, ORG_B, PRJ_B
from app.services import pipeline, retention
from app.services.ingest import Quota
from tests.acceptance.test_t09_deid_review import gray_frame, original, video
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_catalog import _asset
from tests.integration.test_pipeline import drain

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg가 필요합니다")]
ADMIN_A, MEMBER_A, ADMIN_B, VIEWER, OP = 101, 102, 103, 105, 106
CLIPS = "/v1/assets/{asset_id}/clips"


def _job(ctx, job_id: str) -> tuple:  # type: ignore[no-untyped-def]
    with ctx.c.engine.connect() as conn:
        return tuple(conn.execute(text("SELECT status, attempts, last_error_code FROM jobs WHERE job_id = :j"),
                                  {"j": job_id}).one())


def _clip_jobs(ctx) -> list[str]:  # type: ignore[no-untyped-def]
    with ctx.c.engine.connect() as conn:
        return list(conn.execute(text("SELECT job_id FROM jobs WHERE job_type = 'clip_export' ORDER BY created_at"))
                    .scalars())


def _set_room(ctx, room: int, org: str = ORG_A) -> None:  # type: ignore[no-untyped-def]
    """기관 쿼터를 지금 사용량(저장+격리+예약+신청 백업) + room으로 맞춘다."""
    with ctx.c.sessionmaker.begin() as db:
        q = Quota(ctx.c, db)._row(org)
        q.quota_bytes = q.used_bytes + q.quarantine_bytes + q.reserved_bytes + q.backup_bytes + room


def _used(ctx, org: str = ORG_A) -> int:  # type: ignore[no-untyped-def]
    with ctx.c.engine.connect() as conn:
        return int(conn.execute(text("SELECT used_bytes FROM org_quotas WHERE organization_id = :o"),
                                {"o": org}).scalar() or 0)


def _no_clip_assets(ctx) -> None:  # type: ignore[no-untyped-def]
    with ctx.c.sessionmaker() as db:
        assert db.execute(select(Asset).where(Asset.variant == "CLIP")).first() is None
        assert db.execute(select(AssetLineage).where(AssetLineage.relation == "CLIP")).first() is None


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


def _source_b(ctx, data: bytes) -> str:  # type: ignore[no-untyped-def]
    """기관 B의 원 영상 (기관별 한도 확인용, 합성 영상)."""
    with ctx.c.sessionmaker.begin() as db:
        a = _asset(ORG_B, PRJ_B, DEV_B_CAMERA, session=None)
        a.storage_zone, a.storage_key = "RAW", f"{ORG_B}/raw/{a.asset_id}.mp4"
        a.byte_size, a.checksum_sha256 = len(data), hashlib.sha256(data).hexdigest()
        a.ext = {"video_ext": {"resolution": "320x240", "fps": 15, "duration_s": 4}}
        db.add(a)
        aid = a.asset_id
    ctx.c.storage.put_object(ctx.c.settings.bucket_raw, f"{ORG_B}/raw/{aid}.mp4", data, "video/mp4")
    return aid


def test_clip_request_needs_quota_room_and_caps_pending_jobs(ictx, tmp_path: Path) -> None:
    """새 작업은 기관 쿼터에 여유가 있어야 하고(409 QUOTA_EXCEEDED, 업로드와 같은 규칙) 기관의 대기·실행 중 클립 작업은
    CLIP_MAX_PENDING_PER_ORG까지다(429 RATE_LIMITED + Retry-After). 거절하면 작업도 감사도 남기지 않는다."""
    ctx = ictx
    data = video(tmp_path, "testsrc2=size=320x240:rate=15", "a.mp4")
    src = _source(ctx, data)
    path, member = CLIPS.format(asset_id=src), ctx.as_user(MEMBER_A)

    _set_room(ctx, 0)  # 사용량 = 쿼터 (리뷰 재현: 이 상태에서 요청 4건이 모두 202였다)
    r = ctx.client.post(path, json={"start_s": 0, "end_s": 1}, headers=member)
    check(r, "POST", CLIPS)
    body = r.json()
    assert r.status_code == 409 and body["code"] == "QUOTA_EXCEEDED", r.text
    assert body["usage_bytes"] == body["quota_bytes"] and "requested_bytes" not in body
    assert _clip_jobs(ctx) == [] and "CLIP_EXPORT_REQUESTED" not in ctx.audit_actions()

    _set_room(ctx, 10**9)
    ctx.c.settings.__dict__["clip_max_pending_per_org"] = 2
    try:
        first = ctx.client.post(path, json={"start_s": 0, "end_s": 1}, headers=member)
        second = ctx.client.post(path, json={"start_s": 0, "end_s": 1.001}, headers=member)  # 1 ms만 달라도 새 작업
        assert (first.status_code, second.status_code) == (202, 202), (first.text, second.text)
        r = ctx.client.post(path, json={"start_s": 0, "end_s": 1.002}, headers=member)
        check(r, "POST", CLIPS)
        assert r.status_code == 429 and r.json()["code"] == "RATE_LIMITED", r.text
        assert int(r.headers["retry-after"]) >= 1 and (r.json()["pending_jobs"], r.json()["limit"]) == (2, 2)
        # 같은 구간을 다시 누르면 한도와 상관없이 진행 중인 그 작업
        again = ctx.client.post(path, json={"start_s": 0, "end_s": 1}, headers=ctx.as_user(ADMIN_A))
        assert again.status_code == 202 and again.json()["job_id"] == first.json()["job_id"]
        # 한도는 기관별이다
        b = ctx.client.post(CLIPS.format(asset_id=_source_b(ctx, data)), json={"start_s": 0, "end_s": 1},
                            headers=ctx.as_user(ADMIN_B))
        assert b.status_code == 202, b.text
        assert len(_clip_jobs(ctx)) == 3 and ctx.audit_actions().count("CLIP_EXPORT_REQUESTED") == 3
        drain(ctx)  # 작업이 끝나면 다시 등록할 수 있다
        assert ctx.client.post(path, json={"start_s": 0, "end_s": 1.002}, headers=member).status_code == 202
    finally:
        ctx.c.settings.__dict__["clip_max_pending_per_org"] = 3


def test_clip_worker_rechecks_quota_before_storing(ictx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """요청 뒤 쿼터가 차면 작업은 클립을 남기지 않고 바로 FAILED(QUOTA_EXCEEDED, 시도 1번), 사용량은 그대로다.
    올리기 전 확인에서 걸리면 올리지 않고, 올린 뒤 기록할 때(쿼터 행을 잠그고 실제 크기로) 걸리면 올린 객체를 지운다."""
    ctx = ictx
    src = _source(ctx, video(tmp_path, "testsrc2=size=320x240:rate=15", "a.mp4"))
    path, member = CLIPS.format(asset_id=src), ctx.as_user(MEMBER_A)
    real_upload = ctx.c.storage.upload_file
    uploads: list[tuple[str, str]] = []

    def upload(bucket: str, key: str, file_path: str, mime: str) -> None:
        real_upload(bucket, key, file_path, mime)
        uploads.append((bucket, key))

    monkeypatch.setattr(ctx.c.storage, "upload_file", upload)
    j1 = ctx.client.post(path, json={"start_s": 0, "end_s": 1}, headers=member).json()["job_id"]
    used = _used(ctx)
    _set_room(ctx, 0)  # 요청 뒤 다른 업로드가 쿼터를 다 썼다
    drain(ctx)
    assert _job(ctx, j1) == ("FAILED", 1, "QUOTA_EXCEEDED") and uploads == []

    _set_room(ctx, 10**9)
    j2 = ctx.client.post(path, json={"start_s": 1, "end_s": 2}, headers=member).json()["job_id"]

    def upload_while_quota_fills(bucket: str, key: str, file_path: str, mime: str) -> None:
        upload(bucket, key, file_path, mime)
        _set_room(ctx, 0)  # 올리기 전 확인은 통과했지만 올리는 사이 쿼터가 찼다

    monkeypatch.setattr(ctx.c.storage, "upload_file", upload_while_quota_fills)
    drain(ctx)
    assert _job(ctx, j2) == ("FAILED", 1, "QUOTA_EXCEEDED")
    ((bucket, key),) = uploads
    assert key.endswith("/CLIP") and ctx.c.storage.head_object(bucket, key) is None  # 올린 객체를 지웠다
    _no_clip_assets(ctx)
    assert _used(ctx) == used
    r = ctx.client.get(path, headers=member)
    check(r, "GET", CLIPS)
    assert [(x["status"], x.get("error_code")) for x in r.json()["items"]] == [("FAILED", "QUOTA_EXCEEDED")] * 2


def test_clip_request_and_audit_commit_together(ictx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """사유의 제어 문자는 계약대로 400이고 작업을 만들지 않는다. 작업 등록과 감사(CLIP_EXPORT_REQUESTED)는 한
    트랜잭션이라 감사를 남기지 못하면 작업도 남지 않는다 (리뷰 재현: NUL 사유 → 400인데 작업은 대기열에 남았다)."""
    ctx = ictx
    src = _source(ctx, video(tmp_path, "testsrc2=size=320x240:rate=15", "a.mp4"))
    path, member = CLIPS.format(asset_id=src), ctx.as_user(MEMBER_A)
    for bad in ("ab\u0000cd", "줄\n바꿈", "탭\t문자", "del\u007f"):
        r = ctx.client.post(path, json={"start_s": 1, "end_s": 2, "reason": bad}, headers=member)
        check(r, "POST", CLIPS)
        assert r.status_code == 400 and [e["pointer"] for e in r.json()["errors"]] == ["/reason"], (bad, r.text)
    assert _clip_jobs(ctx) == []

    real_record = ctx.c.audit.record

    def record(action: str, **kw: Any) -> str:
        if action == "CLIP_EXPORT_REQUESTED":
            raise RuntimeError("감사 저장 실패 (시험)")
        return real_record(action, **kw)

    monkeypatch.setattr(ctx.c.audit, "record", record)
    r = ctx.client.post(path, json={"start_s": 1, "end_s": 2, "reason": "접근 선박 구간 (합성)"}, headers=member)
    assert r.status_code == 500 and r.json()["code"] == "INTERNAL"
    assert _clip_jobs(ctx) == []  # 작업도 되돌렸다
    monkeypatch.setattr(ctx.c.audit, "record", real_record)
    r = ctx.client.post(path, json={"start_s": 1, "end_s": 2, "reason": "접근 선박 구간 (합성)"}, headers=member)
    check(r, "POST", CLIPS)
    assert r.status_code == 202 and _clip_jobs(ctx) == [r.json()["job_id"]]
    with ctx.c.engine.connect() as conn:
        rows = conn.execute(text("SELECT reason, details->>'job_id' FROM audit_log "
                                 "WHERE action = 'CLIP_EXPORT_REQUESTED'")).all()
    assert [tuple(x) for x in rows] == [("접근 선박 구간 (합성)", r.json()["job_id"])]


@pytest.mark.parametrize("how", ["api", "retention"])
def test_clip_is_not_stored_when_source_is_deleted_first(ictx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                         how: str) -> None:
    """작업이 원 영상을 확인한 뒤·기록하기 전에 원 영상이 지워지면(API 삭제·보존 만료) 클립을 남기지 않는다.
    작업은 원 영상 행을 잠그고 다시 보아 FAILED(SOURCE_UNAVAILABLE)가 되고, 올린 클립 객체를 지운다."""
    ctx = ictx
    src = _source(ctx, video(tmp_path, "testsrc2=size=320x240:rate=15", "a.mp4"))
    job_id = ctx.client.post(CLIPS.format(asset_id=src), json={"start_s": 1, "end_s": 2},
                             headers=ctx.as_user(MEMBER_A)).json()["job_id"]
    real_upload = ctx.c.storage.upload_file
    uploads: list[tuple[str, str]] = []
    certs: list[dict[str, Any]] = []

    def upload_then_delete_source(bucket: str, key: str, file_path: str, mime: str) -> None:
        real_upload(bucket, key, file_path, mime)
        uploads.append((bucket, key))
        if how == "api":
            r = ctx.client.post(f"/v1/assets/{src}/deletion", json={"reason": "기업 요청 삭제 (합성)"},
                                headers=ctx.as_user(ADMIN_A, mfa=True))
            assert r.status_code == 200, r.text
            certs.append(r.json())
        else:
            with ctx.c.engine.begin() as conn:
                conn.execute(text("UPDATE assets SET retention_policy_id = 'raw_default', "
                                  "retention_until = now() - interval '1 day' WHERE asset_id = :a"), {"a": src})
            with ctx.c.sessionmaker.begin() as db:
                certs.extend(retention.certificate_view(x)
                             for x in retention.execute_expired(ctx.c, db, ctx.c.clock.now()))

    monkeypatch.setattr(ctx.c.storage, "upload_file", upload_then_delete_source)
    drain(ctx)
    assert _job(ctx, job_id) == ("FAILED", 1, "SOURCE_UNAVAILABLE")
    ((bucket, key),) = uploads
    assert ctx.c.storage.head_object(bucket, key) is None
    _no_clip_assets(ctx)
    (cert,) = certs
    assert [o["asset_id"] for o in cert["objects"]] == [src]


def _lock_waiters(ctx) -> int:  # type: ignore[no-untyped-def]
    with ctx.c.engine.connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                                     "AND wait_event_type = 'Lock'")).scalar_one())


def test_deletion_waits_for_clip_being_stored_and_includes_it(ictx, tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """작업이 원 영상 행을 잠그고 클립을 기록하는 중에 삭제 요청이 오면, 삭제는 원 자산 행 잠금에서 기다렸다가
    확정된 클립까지 지우고 삭제 증명에 올린다 (원 자산 행을 잠근 뒤 파생본을 찾는다)."""
    ctx = ictx
    src = _source(ctx, video(tmp_path, "testsrc2=size=320x240:rate=15", "a.mp4"))
    job_id = ctx.client.post(CLIPS.format(asset_id=src), json={"start_s": 1, "end_s": 2},
                             headers=ctx.as_user(MEMBER_A)).json()["job_id"]
    real_record = pipeline.record_derived
    race: dict[str, Any] = {}

    def delete_source() -> None:
        race["response"] = ctx.client.post(f"/v1/assets/{src}/deletion", json={"reason": "기업 요청 삭제 (합성)"},
                                           headers=ctx.as_user(ADMIN_A, mfa=True))

    def record_then_delete(*args: Any, **kw: Any) -> None:
        real_record(*args, **kw)  # 클립 행·계보를 넣었고, 이 트랜잭션이 원 영상 행을 잠그고 있다 (아직 커밋 전)
        t = threading.Thread(target=delete_source)
        t.start()
        deadline = time.monotonic() + 20
        while t.is_alive() and not _lock_waiters(ctx) and time.monotonic() < deadline:
            time.sleep(0.05)
        race["thread"], race["deletion_waited"] = t, t.is_alive() and _lock_waiters(ctx) > 0

    monkeypatch.setattr(pipeline, "record_derived", record_then_delete)
    drain(ctx)
    race["thread"].join(timeout=60)
    assert race["deletion_waited"], "삭제가 원 자산 행 잠금을 기다리지 않았다"
    assert _job(ctx, job_id)[0] == "DONE"
    r = race["response"]
    check(r, "POST", "/v1/assets/{asset_id}/deletion")
    assert r.status_code == 200, r.text
    with ctx.c.sessionmaker() as db:
        clip = db.execute(select(Asset).where(Asset.variant == "CLIP")).scalar_one()
    assert clip.deleted_at is not None and clip.deletion_certificate_id == r.json()["certificate_id"]
    objects = {o["asset_id"]: o["object_key"] for o in r.json()["objects"]}
    assert set(objects) == {src, clip.asset_id}
    assert ctx.c.storage.head_object(ctx.c.settings.bucket_proc, objects[clip.asset_id]) is None
