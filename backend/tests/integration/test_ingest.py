"""04번 통합 시험: 사전 서명 멀티파트 업로드 → 완료 → 무결성 검증 → 등록·격리·영수증 (실제 S3 API).

T03(재개)·T04(무결성·격리) 세부 케이스의 축소판이다. 10GB 시험은 tests/acceptance/test_t03_large_resume.py.
"""

from __future__ import annotations

import hashlib
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text

from app.db.models import Asset, Device, OrgQuota, Upload
from app.seed import DEV_A_CAMERA, DEV_A_COLLECTOR, DEV_A_SENSOR, DEV_B_CAMERA, ORG_A, PRJ_A, SES_A
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration
MB = 1024 * 1024


def mp4_bytes(size: int, seed: int = 0) -> bytes:
    head = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
    body = hashlib.sha256(str(seed).encode()).digest() * (size // 32 + 1)
    return (head + body)[:size]


def manifest(data: bytes, *, source: str = DEV_A_CAMERA, data_type: str = "VIDEO", variant: str = "CLIP",
             mime: str = "video/mp4", session: str | None = SES_A, sha: str | None = None, **extra: Any) -> dict:
    m: dict[str, Any] = {
        "schema_version": "1.0.0", "source_system_id": source, "data_type": data_type, "variant": variant,
        "file": {"original_filename": "cam01_0001.mp4", "byte_size": len(data), "mime_type": mime,
                 "checksum_sha256": sha or hashlib.sha256(data).hexdigest()},
        "timestamp_quality": {"source": "DEVICE", "verified": True, "est_offset_ms": 20},
        "capture_start_at": "2026-11-01T01:00:00Z",
        "original_timestamps": {"device_log": "2026-11-01 10:00:00 KST"},
        "client": {"kind": "AGENT", "version": "0.1.0"},
    }
    if session:
        m["session_id"] = session
    m.update(extra)
    return m


def put_parts(created: dict, data: bytes, only: set[int] | None = None) -> list[dict]:
    parts = []
    size = created["part_size"]
    for p in created["parts"] if "parts" in created else created["pending_parts"]:
        n = p["part_number"]
        if only is not None and n not in only:
            continue
        r = httpx.put(p["url"], content=data[(n - 1) * size:n * size], timeout=60)
        assert r.status_code == 200, r.text
        parts.append({"part_number": n, "etag": r.headers["etag"]})
    return parts


def upload_all(ctx, m: dict, data: bytes, headers: dict) -> dict:
    r = ctx.client.post("/v1/uploads", json=m, headers=headers)
    assert r.status_code == 201, r.text
    created = r.json()
    parts = put_parts(created, data)
    r = ctx.client.post(f"/v1/uploads/{created['upload_id']}/complete", json={"parts": parts}, headers=headers)
    assert r.status_code == 202, r.text
    return created


def drain(ctx) -> None:
    """04번 시험: 무결성 검증 작업만 처리한다 (영상 처리 파이프라인은 test_pipeline.py)."""
    from app.services.ingest import IntegrityVerifier

    while (job := ctx.c.queue.claim(["integrity_verify"], "test-worker")) is not None:
        IntegrityVerifier(ctx.c).verify(job.payload["asset_id"])
        ctx.c.queue.complete(job.job_id)


def quota(ctx) -> OrgQuota:
    with ctx.c.sessionmaker() as db:
        return db.get(OrgQuota, ORG_A)


def test_upload_happy_path_and_receipt(ictx) -> None:
    h = ictx.as_user(102)
    data = mp4_bytes(12 * MB)
    r = ictx.client.post("/v1/uploads", json=manifest(data), headers=h)
    check(r, "POST", "/v1/uploads")
    created = r.json()
    assert created["status"] == "UPLOAD_CREATED" and created["part_count"] == 3
    assert quota(ictx).reserved_bytes == len(data)
    parts = put_parts(created, data)
    r = ictx.client.post(f"/v1/uploads/{created['upload_id']}/complete", json={"parts": parts}, headers=h)
    check(r, "POST", "/v1/uploads/{upload_id}/complete")
    assert r.json()["ingest_status"] == "RECEIVED"  # 수신은 등록이 아니다
    assert ictx.client.get(f"/v1/assets/{created['asset_id']}/receipt", headers=h).status_code == 404
    drain(ictx)
    st = ictx.client.get(f"/v1/uploads/{created['upload_id']}", headers=h)
    check(st, "GET", "/v1/uploads/{upload_id}")
    assert st.json()["ingest_status"] == "REGISTERED" and st.json()["parts_completed"] == 3
    rc = ictx.client.get(f"/v1/assets/{created['asset_id']}/receipt", headers=h)
    check(rc, "GET", "/v1/assets/{asset_id}/receipt")
    assert rc.json()["checksum_sha256"] == hashlib.sha256(data).hexdigest()
    a = ictx.client.get(f"/v1/assets/{created['asset_id']}", headers=h)
    check(a, "GET", "/v1/assets/{asset_id}")
    body = a.json()
    assert body["capture_start_at"] == "2026-11-01T01:00:00Z" and body["sharing_scope"] == "PRIVATE"
    assert body["data_role"] == "CORE" and body["privacy_status"] == "REVIEW_REQUIRED"
    assert body["provenance"]["original_timestamps"]["device_log"] == "2026-11-01 10:00:00 KST"
    assert "storage_key" not in a.text and "cam01_0001" not in body.get("storage", "")
    q = quota(ictx)
    assert (q.reserved_bytes, q.used_bytes) == (0, len(data))
    # 객체 키에 원래 파일명이 들어가지 않는다 (domain.md 9장)
    with ictx.c.sessionmaker() as db:
        key = db.get(Asset, created["asset_id"]).storage_key
    assert key.startswith(f"{ORG_A}/{PRJ_A}/{SES_A}/{DEV_A_CAMERA}/") and "cam01" not in key
    assert "ASSET_VIEWED" in ictx.audit_actions()
    lst = ictx.client.get("/v1/assets?data_type=VIDEO", headers=h)
    check(lst, "GET", "/v1/assets")
    assert [i["asset_id"] for i in lst.json()["items"]] == [created["asset_id"]]


def test_T03_resume_after_interruption(ictx) -> None:
    h = ictx.as_user(102)
    data = mp4_bytes(11 * MB, seed=1)
    created = ictx.client.post("/v1/uploads", json=manifest(data), headers=h).json()
    first = put_parts(created, data, only={1})  # 1번 파트만 보내고 끊김
    r = ictx.client.get(f"/v1/uploads/{created['upload_id']}/parts", headers=h)
    check(r, "GET", "/v1/uploads/{upload_id}/parts")
    pl = r.json()
    assert [p["part_number"] for p in pl["completed_parts"]] == [1]
    assert [p["part_number"] for p in pl["pending_parts"]] == [2, 3]
    rest = put_parts(pl, data)
    r = ictx.client.post(f"/v1/uploads/{created['upload_id']}/complete", json={"parts": first + rest}, headers=h)
    assert r.status_code == 202
    drain(ictx)
    assert ictx.client.get(f"/v1/uploads/{created['upload_id']}", headers=h).json()["ingest_status"] == "REGISTERED"


def test_T04_checksum_mismatch_is_quarantined(ictx) -> None:
    h = ictx.as_user(102)
    data = mp4_bytes(6 * MB, seed=2)
    declared = hashlib.sha256(b"something else").hexdigest()
    created = upload_all(ictx, manifest(data, sha=declared), data, h)
    drain(ictx)
    st = ictx.client.get(f"/v1/uploads/{created['upload_id']}", headers=h).json()
    assert (st["ingest_status"], st["quarantine_reason"]) == ("QUARANTINED", "CHECKSUM_MISMATCH")
    with ictx.c.sessionmaker() as db:
        a = db.get(Asset, created["asset_id"])
    assert a.storage_zone == "QUARANTINE"
    assert ictx.c.storage.head_object(ictx.c.settings.bucket_quarantine, a.storage_key) is not None
    assert ictx.c.storage.head_object(ictx.c.settings.bucket_proc, a.storage_key) is None
    assert any(n.kind == "upload.quarantined" for n in ictx.c.notifier.sent)
    q = quota(ictx)
    assert (q.reserved_bytes, q.quarantine_bytes, q.used_bytes) == (0, len(data), 0)
    body = ictx.client.get(f"/v1/assets/{created['asset_id']}", headers=h)
    check(body, "GET", "/v1/assets/{asset_id}")


def test_T04_mime_mismatch_detected_by_content(ictx) -> None:
    h = ictx.as_user(102)
    data = b"time,temp\n2026-11-01T00:00:00Z,18.4\n" * 1000
    created = upload_all(ictx, manifest(data, mime="video/mp4"), data, h)
    drain(ictx)
    st = ictx.client.get(f"/v1/uploads/{created['upload_id']}", headers=h).json()
    assert st["quarantine_reason"] == "MIME_MISMATCH"


def test_unknown_session_quarantine_then_assign_and_revalidate(ictx) -> None:
    h = ictx.as_user(102)
    data = b"time,temp\n2026-11-01T00:00:00Z,18.4\n"
    m = manifest(data, source=DEV_A_SENSOR, data_type="SENSOR", variant="ORIGINAL", mime="text/csv", session=None,
                 timestamp_quality={"source": "UNKNOWN", "verified": False})
    m.pop("capture_start_at")
    created = upload_all(ictx, m, data, h)
    drain(ictx)
    a = ictx.client.get(f"/v1/assets/{created['asset_id']}", headers=h).json()
    assert (a["ingest_status"], a["quarantine_reason"]) == ("QUARANTINED", "UNKNOWN_SESSION")
    assert a["quality_status"] == "PENDING" and a.get("session_id") is None
    with ictx.c.sessionmaker() as db:
        retention_before = db.get(Asset, created["asset_id"]).retention_until
    assert retention_before.year > 2030  # 회차 미상은 자동 삭제하지 않는다 (domain.md 9장)
    r = ictx.client.patch(f"/v1/assets/{created['asset_id']}", json={"session_id": SES_A}, headers=h)
    check(r, "PATCH", "/v1/assets/{asset_id}")
    assert r.json()["ingest_status"] == "VALIDATING"
    drain(ictx)
    a = ictx.client.get(f"/v1/assets/{created['asset_id']}", headers=h).json()
    assert a["ingest_status"] == "READY_PRIVATE" and "quarantine_reason" not in a
    assert a["quality_status"] == "WARNING" and a["quality_reasons"] == ["capture_time_unknown"]
    with ictx.c.sessionmaker() as db:
        asset = db.get(Asset, created["asset_id"])
    assert asset.storage_zone == "RAW" and f"/{SES_A}/" in asset.storage_key
    q = quota(ictx)
    assert (q.quarantine_bytes, q.used_bytes) == (0, len(data))


def test_duplicates_and_idempotency(ictx) -> None:
    h = ictx.as_user(102)
    data = mp4_bytes(5 * MB, seed=3)
    m = manifest(data)
    created = upload_all(ictx, m, data, h)
    drain(ictx)
    r = ictx.client.post("/v1/uploads", json=m, headers=h)
    check(r, "POST", "/v1/uploads")
    assert r.status_code == 200 and r.json() == {"status": "ALREADY_REGISTERED", "asset_id": created["asset_id"]}
    data2 = mp4_bytes(5 * MB, seed=4)
    m2 = manifest(data2)
    k = {**h, "Idempotency-Key": "agent-file-0002"}
    first = ictx.client.post("/v1/uploads", json=m2, headers=k)
    again = ictx.client.post("/v1/uploads", json=m2, headers=k)
    assert again.headers.get("idempotent-replayed") == "true"
    assert again.json()["upload_id"] == first.json()["upload_id"]
    other = ictx.client.post("/v1/uploads", json=manifest(mp4_bytes(5 * MB, seed=5)), headers=k)
    assert other.status_code == 409 and other.json()["code"] == "IDEMPOTENCY_CONFLICT"
    with ictx.c.sessionmaker() as db:
        assert len(db.execute(select(Upload)).scalars().all()) == 2


def test_variant_rules_and_metadata_only(ictx) -> None:
    h = ictx.as_user(102)
    data = mp4_bytes(5 * MB, seed=6)
    r = ictx.client.post("/v1/uploads", json=manifest(data, variant="ORIGINAL"), headers=h)  # C2 장비에 원본
    check(r, "POST", "/v1/uploads")
    assert r.status_code == 422 and r.json()["code"] == "RULE_VIOLATION"
    assert r.json()["allowed_variants"] == ["CLIP", "PROXY", "SAMPLE_FRAME"]
    with ictx.c.sessionmaker.begin() as db:
        db.get(Device, DEV_A_CAMERA).collection_mode = "C0_METADATA_ONLY"
    r = ictx.client.post("/v1/uploads", json=manifest(data, variant="ORIGINAL"), headers=h)
    assert r.status_code == 201 and r.json()["status"] == "METADATA_ONLY_REGISTERED" and "parts" not in r.json()
    a = ictx.client.get(f"/v1/assets/{r.json()['asset_id']}", headers=h)
    check(a, "GET", "/v1/assets/{asset_id}")
    assert a.json()["has_binary"] is False and a.json()["ingest_status"] == "READY_PRIVATE"
    assert a.json()["byte_size"] == len(data)
    # 정책이 바뀌어 원본을 받을 수 있게 되면 같은 asset_id로 세션을 연다 (요구사항 2.4)
    with ictx.c.sessionmaker.begin() as db:
        db.get(Device, DEV_A_CAMERA).collection_mode = "C3_APPROVED_FULL"
    r2 = ictx.client.post("/v1/uploads", json=manifest(data, variant="ORIGINAL"), headers=h)
    assert r2.status_code == 201 and r2.json()["asset_id"] == r.json()["asset_id"]
    # 취소하면 바이너리 없는 C0 기록으로 돌아간다
    assert ictx.client.delete(f"/v1/uploads/{r2.json()['upload_id']}", headers=h).status_code == 204
    back = ictx.client.get(f"/v1/assets/{r.json()['asset_id']}", headers=h).json()
    assert back["ingest_status"] == "READY_PRIVATE" and back["has_binary"] is False


def test_quota_abort_and_expiry(ictx) -> None:
    h = ictx.as_user(102)
    data = mp4_bytes(5 * MB, seed=7)
    ictx.client.post("/v1/uploads", json=manifest(mp4_bytes(5 * MB, seed=70)), headers=h)  # 쿼터 행 생성
    with ictx.c.sessionmaker.begin() as db:
        db.get(OrgQuota, ORG_A).quota_bytes = 8 * MB
    r = ictx.client.post("/v1/uploads", json=manifest(data), headers=h)
    assert r.status_code == 409 and r.json()["code"] == "QUOTA_EXCEEDED"
    with ictx.c.sessionmaker.begin() as db:
        db.get(OrgQuota, ORG_A).quota_bytes = 100 * MB
    created = ictx.client.post("/v1/uploads", json=manifest(data), headers=h).json()
    assert quota(ictx).reserved_bytes == 10 * MB
    assert ictx.client.delete(f"/v1/uploads/{created['upload_id']}", headers=h).status_code == 204
    assert quota(ictx).reserved_bytes == 5 * MB
    assert ictx.client.get(f"/v1/assets/{created['asset_id']}", headers=h).status_code == 404  # 받은 것 없는 기록 삭제
    # 만료: 410 + 예약 해제
    again = ictx.client.post("/v1/uploads", json=manifest(data), headers=h).json()
    with ictx.c.engine.begin() as conn:
        conn.execute(text("UPDATE uploads SET expires_at = now() - interval '1 minute' WHERE upload_id = :u"),
                     {"u": again["upload_id"]})
    r = ictx.client.get(f"/v1/uploads/{again['upload_id']}/parts", headers=h)
    assert r.status_code == 410 and r.json()["code"] == "GONE"
    assert quota(ictx).reserved_bytes == 5 * MB
    # 클라이언트가 다시 오지 않아도 워커 주기 정리가 만료 세션을 정리한다
    from app.workers.main import sweep

    first = ictx.client.get("/v1/assets", headers=h).json()["items"]
    with ictx.c.engine.begin() as conn:
        conn.execute(text("UPDATE uploads SET expires_at = now() - interval '1 minute' WHERE status = 'UPLOADING'"))
    assert sweep(ictx.c) == 1
    assert quota(ictx).reserved_bytes == 0
    assert len(ictx.client.get("/v1/assets", headers=h).json()["items"]) == len(first) - 1


def test_device_principal_scope_and_cross_org(ictx) -> None:
    code = ictx.client.post(f"/v1/devices/{DEV_A_COLLECTOR}/enrollment-codes",
                            headers=ictx.as_user(101, mfa=True)).json()["code"]
    cred = ictx.client.post("/v1/agents/enroll", json={"enrollment_code": code, "agent_version": "0.1.0",
                                                       "os": "linux"}).json()
    tok = ictx.client.post("/v1/auth/token", data={"grant_type": "client_credentials", "client_id": cred["client_id"],
                                                   "client_secret": cred["client_secret"]}).json()["access_token"]
    dh = {"Authorization": f"Bearer {tok}"}
    data = mp4_bytes(5 * MB, seed=8)
    created = upload_all(ictx, manifest(data), data, dh)
    assert ictx.client.post("/v1/uploads", json=manifest(data, source=DEV_B_CAMERA), headers=dh).status_code == 404
    # B 기관 사용자는 A 업로드·자산을 볼 수 없다 (T01)
    hb = ictx.as_user(104)
    assert ictx.client.get(f"/v1/uploads/{created['upload_id']}", headers=hb).status_code == 404
    assert ictx.client.get(f"/v1/assets/{created['asset_id']}", headers=hb).status_code == 404
    assert ictx.client.get(f"/v1/assets/{created['asset_id']}/receipt", headers=hb).status_code == 404
    assert ictx.client.get("/v1/assets", headers=hb).json()["items"] == []
    assert ictx.client.patch(f"/v1/assets/{created['asset_id']}", json={"location_precision": "NONE"},
                             headers=hb).status_code == 404


def test_validation_errors_on_create(ictx) -> None:
    h = ictx.as_user(102)
    data = mp4_bytes(5 * MB, seed=9)
    bad = manifest(data, location={"type": "Point", "coordinates": [35.1, 129.1]})
    r = ictx.client.post("/v1/uploads", json=bad, headers=h)
    check(r, "POST", "/v1/uploads")
    assert r.status_code == 400 and r.json()["errors"][0]["code"] == "lonlat_swapped"
    r = ictx.client.post("/v1/uploads", json={**manifest(data), "schema_version": "2.0.0"}, headers=h)
    assert r.status_code == 422 and r.json()["code"] == "UNSUPPORTED_SCHEMA_VERSION"
    r = ictx.client.post("/v1/uploads", json={**manifest(data), "organization_id": ORG_A}, headers=h)
    assert r.status_code == 400  # 계약에 없는 필드


def test_source_location_is_transformed_to_4326(ictx) -> None:
    h = ictx.as_user(102)
    data = mp4_bytes(5 * MB, seed=10)
    m = manifest(data, source_location={"crs": "EPSG:3857",
                                        "geometry": {"type": "Point", "coordinates": [14080000.0, 4160000.0]}})
    created = ictx.client.post("/v1/uploads", json=m, headers=h).json()
    a = ictx.client.get(f"/v1/assets/{created['asset_id']}", headers=h).json()
    lon, lat = a["location"]["coordinates"]
    assert 126.4 < lon < 126.6 and 34.8 < lat < 35.1
    assert a["source_location"]["crs"] == "EPSG:3857" and a["location_precision"] == "EXACT_RESTRICTED"
    with ictx.c.sessionmaker() as db:
        assert db.get(Asset, created["asset_id"]).ingest_context["location_transform"]["from_crs"] == "EPSG:3857"
