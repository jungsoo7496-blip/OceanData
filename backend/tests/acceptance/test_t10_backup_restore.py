"""T10 백업·복원 (docs/ACCEPTANCE.md, 08번 요구사항 6·7, ADR-0021).

합성 자산(원본 NONE·원본 PROTECTED·비식별본)과 증빙·데이터셋 버킷 객체 → 별도 백업 버킷 증분 복사 →
pg_dump 스냅샷 → 임시 DB·임시 위치 복원 → 행 수·감사 로그 체인·sha256 대조. 손상은 원본·백업 사본 양쪽에서.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import secrets
import shutil

import pytest

from app.db.models import BackupRun
from app.seed import DEV_A_CAMERA, ORG_A, PRJ_A
from app.services import backup, inspections
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_catalog import _asset

pytestmark = [pytest.mark.integration, pytest.mark.acceptance,
              pytest.mark.skipif(not shutil.which("pg_dump") or not shutil.which("pg_restore"),
                                 reason="pg_dump·pg_restore가 없습니다 (서버와 같은 주버전)")]
OP, ADMIN_A = 106, 101


def _put_asset(ctx, *, variant: str, zone: str, backup_class: str = "NONE") -> tuple[str, str, bytes]:
    data = secrets.token_bytes(4096)
    with ctx.c.sessionmaker.begin() as db:
        a = _asset(ORG_A, PRJ_A, DEV_A_CAMERA, variant=variant)
        a.storage_zone, a.backup_class = zone, backup_class
        a.storage_key = f"{ORG_A}/{a.asset_id}/{variant}"
        a.byte_size, a.checksum_sha256 = len(data), hashlib.sha256(data).hexdigest()
        db.add(a)
        aid, key = a.asset_id, a.storage_key
    ctx.c.storage.put_object(ctx.c.settings.buckets[zone], key, data, "video/mp4")
    return aid, key, data


@pytest.mark.covers("T10-a", "T10-b")
def test_t10_tiered_backup_and_restore(ictx) -> None:
    ctx = ictx
    c = dataclasses.replace(ctx.c, settings=ctx.c.settings.model_copy(
        update={"backup_bucket": f"{ctx.c.settings.bucket_raw[:-4]}-backup", "backup_enabled": True}))
    _, raw_none_key, _ = _put_asset(ctx, variant="ORIGINAL", zone="RAW")  # backup_class=NONE → 제외
    _, raw_prot_key, _ = _put_asset(ctx, variant="ORIGINAL", zone="RAW", backup_class="PROTECTED")
    _, deid_key, _ = _put_asset(ctx, variant="DEID", zone="PROCESSED")
    c.storage.put_object(c.settings.bucket_evidence, "reports/rpt_x/snapshot.json", b'{"ok":1}', "application/json")
    c.storage.put_object(c.settings.bucket_datasets, "dst_x/dsv_x/MANIFEST.json", b'{"items":[]}', "application/json")

    # T10-b: 백업 대상 — PROTECTED·DEID·EVIDENCE·DATASET 포함, NONE인 RAW 제외
    with c.sessionmaker() as db:
        planned = {(i["zone"], i["key"]) for i in backup.plan(c, db)}
    assert ("RAW", raw_prot_key) in planned and ("PROCESSED", deid_key) in planned
    assert ("EVIDENCE", "reports/rpt_x/snapshot.json") in planned and ("DATASET", "dst_x/dsv_x/MANIFEST.json") in planned
    assert ("RAW", raw_none_key) not in planned

    rid = backup.run(c)
    with c.sessionmaker() as db:
        r = db.get(BackupRun, rid)
        assert r is not None and r.status == "SUCCEEDED", (r.error, r.failed_objects)
        assert r.object_count == len(planned) == r.copied_count and r.table_count and r.table_count > 30
    manifest = json.loads(c.storage.get_object(c.settings.backup_bucket, f"runs/{rid}/manifest.json"))
    keys = {o["backup_key"] for o in manifest["objects"]}
    assert f"objects/RAW/{raw_prot_key}" in keys and f"objects/RAW/{raw_none_key}" not in keys
    assert c.storage.head_object(c.settings.backup_bucket, f"objects/RAW/{raw_none_key}") is None
    assert manifest["db"]["tables"]["assets"] >= 3 and manifest["db"]["tables"]["audit_log"] >= 0

    # 증분: 바뀐 게 없으면 다시 복사하지 않는다
    rid2 = backup.run(c)
    with c.sessionmaker() as db:
        r2 = db.get(BackupRun, rid2)
        assert r2 is not None and r2.status == "SUCCEEDED" and r2.copied_count == 0
        assert r2.skipped_count == r2.object_count
        # D_BACKUP 정기 점검 자동 판정 (요구사항 6.3)
        assert inspections.auto_fill(c, db, "DAILY", c.clock.now())["D_BACKUP"][0] == "PASS"

    # 운영자 조회·지금 백업 (계약)
    r = ctx.client.get("/v1/backup-runs", headers=ctx.as_user(OP))
    check(r, "GET", "/v1/backup-runs")
    assert [x["backup_run_id"] for x in r.json()["items"]][:2] == [rid2, rid]
    assert ctx.client.get("/v1/backup-runs", headers=ctx.as_user(ADMIN_A)).status_code == 403
    r = ctx.client.post("/v1/backup-runs", headers=ctx.as_user(OP))
    check(r, "POST", "/v1/backup-runs")
    assert r.status_code == 202 and "BACKUP_REQUESTED" in ctx.audit_actions()

    # T10-a: 임시 DB·임시 위치에 복원 → 행 수·감사 로그 체인·객체 해시가 원본과 같다
    rep = backup.restore_test(c)
    assert rep["ok"], rep
    assert rep["backup_run_id"] == rid2 and rep["tables_checked"] == r2.table_count
    assert rep["objects_checked"] == r2.object_count and rep["audit_chain_ok"] is True
    assert c.storage.head_object(c.settings.backup_bucket, f"restore-tests/{rid2}/RAW/{raw_prot_key}") is None
    r = ctx.client.get("/v1/backup-runs", headers=ctx.as_user(OP))
    check(r, "GET", "/v1/backup-runs")
    assert r.json()["items"][0]["restore_test"]["ok"] is True

    # 백업 사본 손상 → 복원 시험이 잡는다
    c.storage.put_object(c.settings.backup_bucket, f"objects/PROCESSED/{deid_key}", b"tampered", "video/mp4")
    rep = backup.restore_test(c)
    assert not rep["ok"] and [m["key"] for m in rep["object_mismatches"]] == [f"objects/PROCESSED/{deid_key}"]

    # 손상된 백업 사본은 다음 회차에 원본에서 다시 복사한다. 그때 원본도 기록(sha256)과 다르면 복사하지 않고
    # 실패로 기록 + 운영자 알림, 정기 점검 FAIL (원본 손상 자체의 상시 감지는 05번 verify-integrity 표본 검사)
    c.storage.put_object(c.settings.bucket_proc, deid_key, b"bit rot", "video/mp4")
    rid3 = backup.run(c)
    with c.sessionmaker() as db:
        r3 = db.get(BackupRun, rid3)
        assert r3 is not None and r3.status == "FAILED"
        assert [f["reason"] for f in r3.failed_objects] == ["원본 sha256이 기록과 다름 (손상 의심, 복사하지 않음)"]
        assert inspections.auto_fill(c, db, "DAILY", c.clock.now())["D_BACKUP"][0] == "FAIL"
    assert any(n.kind == "backup.failed" and rid3 in n.subject for n in c.notifier.sent)
