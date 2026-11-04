"""05번 통합 시험: 메타 추출 → 프록시 → 썸네일 → READY_PRIVATE, 손상 격리, 큐(재시도·임대·자원 클래스),
재처리 API, 무결성 로그 앵커·표본 검증. 실제 ffmpeg와 S3 API(moto 또는 SeaweedFS)를 쓴다."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select, text

from app.adapters.system_clock import FixedClock
from app.db.models import Asset, AssetLineage, IntegrityAnchor, JobRow, OrgQuota
from app.seed import DEV_A_CAMERA, ORG_A, SES_A
from app.services.integrity import anchor_previous_days, verify_integrity
from app.workers import main as worker
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg가 필요합니다")]


def synth_video(tmp: Path, *, seconds: int = 3, size: str = "320x240", creation: str | None = "2026-11-01T01:02:03Z",
                gps: str | None = None) -> bytes:
    out = tmp / "v.mp4"
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
            f"testsrc2=size={size}:rate=15", "-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if creation:
        args += ["-metadata", f"creation_time={creation}"]
    else:
        args += ["-map_metadata", "-1", "-fflags", "+bitexact"]
    if gps:
        args += ["-metadata", f"location={gps}"]
    subprocess.run([*args, str(out)], check=True)
    return out.read_bytes()


def upload(ctx, data: bytes, *, tq_source: str = "UNKNOWN") -> str:
    h = ctx.as_user(102)
    m = {"schema_version": "1.0.0", "source_system_id": DEV_A_CAMERA, "data_type": "VIDEO", "variant": "CLIP",
         "session_id": SES_A, "file": {"original_filename": "c.mp4", "byte_size": len(data), "mime_type": "video/mp4",
                                       "checksum_sha256": hashlib.sha256(data).hexdigest()},
         "timestamp_quality": {"source": tq_source, "verified": False}, "client": {"kind": "API", "version": "t"}}
    created = ctx.client.post("/v1/uploads", json=m, headers=h).json()
    parts = []
    for p in created["parts"]:
        n, size = p["part_number"], created["part_size"]
        r = httpx.put(p["url"], content=data[(n - 1) * size:n * size], timeout=60)
        parts.append({"part_number": n, "etag": r.headers["etag"]})
    assert ctx.client.post(f"/v1/uploads/{created['upload_id']}/complete", json={"parts": parts},
                           headers=h).status_code == 202
    return created["asset_id"]


def drain(ctx) -> None:
    while worker.run_once(ctx.c, "test"):
        pass


def jobs(ctx) -> list[tuple]:
    with ctx.c.engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(
            "SELECT job_type, status, attempts, last_error_code FROM jobs ORDER BY created_at"))]


def test_video_pipeline_to_ready_private(ictx, tmp_path) -> None:
    data = synth_video(tmp_path, gps="+35.1000+129.0500/")
    asset_id = upload(ictx, data)
    drain(ictx)
    h = ictx.as_user(102)
    a = ictx.client.get(f"/v1/assets/{asset_id}", headers=h)
    check(a, "GET", "/v1/assets/{asset_id}")
    body = a.json()
    assert body["ingest_status"] == "READY_PRIVATE", jobs(ictx)
    ve = body["video_ext"]
    assert ve["resolution"] == "320x240" and ve["fps"] == 15 and ve["codec"] == "h264" and 2.9 < ve["duration_s"] < 3.1
    # 컨테이너 생성 시각 태그 → DEVICE, verified=false (05번 요구사항 3.2)
    assert body["capture_start_at"] == "2026-11-01T01:02:03Z"
    assert body["timestamp_quality"] == {"source": "DEVICE", "verified": False, "est_offset_ms": None}
    assert body["location"] == {"type": "Point", "coordinates": [129.05, 35.1]}
    lin = ictx.client.get(f"/v1/assets/{asset_id}/lineage", headers=h).json()
    rels = {e["relation"]: e for e in lin["descendants"]}
    assert set(rels) == {"PROXY", "THUMBNAIL"} and rels["PROXY"]["tool_version"].startswith("ffmpeg")
    proxy = ictx.client.get(f"/v1/assets/{rels['PROXY']['asset_id']}", headers=h).json()
    thumb = ictx.client.get(f"/v1/assets/{rels['THUMBNAIL']['asset_id']}", headers=h).json()
    assert (proxy["variant"], proxy["mime_type"], proxy["sharing_scope"]) == ("PROXY", "video/mp4", "PRIVATE")
    assert (thumb["variant"], thumb["data_type"], thumb["mime_type"]) == ("THUMBNAIL", "IMAGE", "image/jpeg")
    assert proxy["provenance"]["parent_asset_ids"] == [asset_id] and proxy["data_role"] == body["data_role"]
    # 프록시는 원본보다 크게 키우지 않는다 (짧은 변 240 < 480)
    with ictx.c.sessionmaker() as db:
        pa = db.get(Asset, proxy["asset_id"])
        raw = ictx.c.storage.get_object(ictx.c.settings.bucket_proc, pa.storage_key)
    (tmp_path / "p.mp4").write_bytes(raw)
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
                                       str(tmp_path / "p.mp4")], capture_output=True, check=True).stdout)
    assert probe["streams"][0]["height"] == 240
    assert ictx.client.get(f"/v1/assets/{proxy['asset_id']}/receipt", headers=h).status_code == 200
    rep = verify_integrity(ictx.c, sample_size=3, seed=1)
    assert rep.ok and rep.checked_entries == 3 and rep.sampled == 3
    with ictx.c.sessionmaker() as db:
        q = db.get(OrgQuota, ORG_A)
        sizes = sum(x.byte_size for x in db.execute(select(Asset)).scalars())
    assert q.used_bytes == sizes  # PROCESSED 존 파생본도 사용량에 들어간다


def test_corrupt_video_is_quarantined(ictx) -> None:
    data = b"\x00\x00\x00\x18ftypmp42" + b"\x00garbage" * 50_000
    asset_id = upload(ictx, data)
    drain(ictx)
    a = ictx.client.get(f"/v1/assets/{asset_id}", headers=ictx.as_user(102)).json()
    assert (a["ingest_status"], a["quarantine_reason"]) == ("QUARANTINED", "CORRUPT_FILE")
    assert a["quality_status"] == "FAILED"
    with ictx.c.sessionmaker() as db:
        q = db.get(OrgQuota, ORG_A)
    assert (q.used_bytes, q.quarantine_bytes) == (0, len(data))


def test_queue_retry_lease_and_resource_classes(ictx) -> None:
    c = ictx.c
    calls: list[str] = []
    worker.register("test_fail", lambda c, job: (_ for _ in ()).throw(RuntimeError("boom")),
                    lambda c, job, code: calls.append(code))
    try:
        with c.engine.begin() as conn:
            conn.execute(text("INSERT INTO job_policies (job_type, priority, max_attempts) VALUES ('test_fail', 1, 2)"))
        jid = c.queue.enqueue("test_fail", {})
        assert worker.run_once(c, "w")  # 1회 실패 → 재시도 예약 (지수 백오프)
        with c.engine.begin() as conn:
            row = conn.execute(text("SELECT status, attempts, run_after > now() FROM jobs WHERE job_id=:j"),
                               {"j": jid}).one()
            assert tuple(row) == ("QUEUED", 1, True)
            conn.execute(text("UPDATE jobs SET run_after = now() WHERE job_id=:j"), {"j": jid})
        assert worker.run_once(c, "w")  # 2회째 실패 → 최대 시도 → FAILED + on_exhausted
        with c.sessionmaker() as db:
            job = db.get(JobRow, jid)
            assert (job.status, job.last_error_code) == ("FAILED", "INTERNAL")
        assert calls == ["INTERNAL"]
    finally:
        worker.HANDLERS.pop("test_fail")
    # 임대 만료: 멈춘 워커의 작업을 다시 대기로 (요구사항 1.3)
    jid = c.queue.enqueue("noop", {"asset_id": None})
    job = c.queue.claim(["noop"], "dead-worker")
    assert job is not None and job.job_id == jid
    with c.engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE job_id=:j"), {"j": jid})
    assert c.queue.reap_expired_leases() == 1
    assert c.queue.claim(["noop"], "w2").job_id == jid
    # 자원 클래스: tag(GPU, 없으면 대기)는 CPU 워커가 가져가지 않고, deid(GPU, 없으면 CPU)는 가져간다
    tag = c.queue.enqueue("tag", {})
    deid = c.queue.enqueue("deid", {})
    got = c.queue.claim(["tag", "deid"], "cpu", ("CPU",))
    assert got is not None and got.job_id == deid
    assert c.queue.claim(["tag"], "cpu", ("CPU",)) is None
    assert c.queue.claim(["tag"], "gpu", ("CPU", "GPU")).job_id == tag
    # 우선순위는 정책 표에서 (DEID > PROXY > TAG …)
    with c.sessionmaker() as db:
        prios = dict(db.execute(select(JobRow.job_type, JobRow.priority).where(JobRow.job_id.in_([tag, deid]))).all())
    assert prios["deid"] > prios["tag"]


def test_reprocess_api(ictx, tmp_path) -> None:
    asset_id = upload(ictx, synth_video(tmp_path, seconds=2, creation=None))
    drain(ictx)
    body = {"steps": ["THUMBNAIL", "PROXY"], "reason": "프록시 프로파일 변경"}
    assert ictx.client.post(f"/v1/assets/{asset_id}/reprocess", json=body,
                            headers=ictx.as_user(106)).json()["code"] == "MFA_REQUIRED"
    assert ictx.client.post(f"/v1/assets/{asset_id}/reprocess", json=body,
                            headers=ictx.as_user(101, mfa=True)).status_code == 403
    r = ictx.client.post(f"/v1/assets/{asset_id}/reprocess", json=body, headers=ictx.as_user(106, mfa=True))
    check(r, "POST", "/v1/assets/{asset_id}/reprocess")
    assert r.status_code == 202
    with ictx.c.sessionmaker() as db:
        first = db.execute(select(JobRow).where(JobRow.job_id == r.json()["job_ids"][0])).scalar_one()
    assert first.job_type == "make_proxy" and first.payload["chain"] == ["make_thumbnail"]  # 단계 순서대로
    assert "REPROCESS_REQUESTED" in ictx.audit_actions()
    drain(ictx)
    a = ictx.client.get(f"/v1/assets/{asset_id}", headers=ictx.as_user(102)).json()
    assert a["ingest_status"] == "READY_PRIVATE" and a["timestamp_quality"]["source"] == "UNKNOWN"
    # FAILED 자산은 실패 직전 단계로 돌린 뒤 재처리
    with ictx.c.engine.begin() as conn:
        conn.execute(text("UPDATE assets SET ingest_status='FAILED', ingest_context = ingest_context || "
                          "'{\"failed_from\": \"REGISTERED\"}'::jsonb WHERE asset_id=:a"), {"a": asset_id})
    r = ictx.client.post(f"/v1/assets/{asset_id}/reprocess", json={"steps": ["PROXY"], "reason": "재시도"},
                         headers=ictx.as_user(106, mfa=True))
    assert r.status_code == 202
    with ictx.c.sessionmaker() as db:
        assert db.get(Asset, asset_id).ingest_status == "REGISTERED"


def test_integrity_anchor_and_tamper_detection(ictx) -> None:
    data = b"time,temp\n2026-11-01T00:00:00Z,18.4\n"
    h = ictx.as_user(102)
    m = {"schema_version": "1.0.0", "source_system_id": "dev_" + "0" * 23 + "303", "data_type": "SENSOR",
         "variant": "ORIGINAL", "session_id": SES_A, "timestamp_quality": {"source": "UNKNOWN", "verified": False},
         "file": {"original_filename": "a.csv", "byte_size": len(data), "mime_type": "text/csv",
                  "checksum_sha256": hashlib.sha256(data).hexdigest()}, "client": {"kind": "API", "version": "t"}}
    created = ictx.client.post("/v1/uploads", json=m, headers=h).json()
    r = httpx.put(created["parts"][0]["url"], content=data)
    ictx.client.post(f"/v1/uploads/{created['upload_id']}/complete",
                     json={"parts": [{"part_number": 1, "etag": r.headers["etag"]}]}, headers=h)
    drain(ictx)
    assert verify_integrity(ictx.c, sample_size=1).ok
    # 내일 시점에서 오늘 앵커를 만든다 (요구사항 5.2)
    tomorrow = dataclasses.replace(ictx.c, clock=FixedClock(ictx.c.clock.now() + timedelta(days=1)))
    assert anchor_previous_days(tomorrow) == 1 and anchor_previous_days(tomorrow) == 0
    with ictx.c.sessionmaker() as db:
        anchor = db.execute(select(IntegrityAnchor)).scalar_one()
    assert anchor.entry_count == 1 and len(anchor.anchor_hash) == 64
    # 저장된 객체가 바뀌면 표본 검증이 불일치를 보고한다 (요구사항 5.3)
    with ictx.c.sessionmaker() as db:
        a = db.get(Asset, created["asset_id"])
    ictx.c.storage.put_object(ictx.c.settings.bucket_raw, a.storage_key, b"tampered")
    rep = verify_integrity(ictx.c, sample_size=1)
    assert not rep.ok and rep.mismatched_assets == [created["asset_id"]]
    assert ictx.c.storage.head_object(ictx.c.settings.bucket_raw, a.storage_key) is not None
    with ictx.c.sessionmaker() as db:
        assert db.execute(select(AssetLineage)).first() is None
