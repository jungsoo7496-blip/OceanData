"""T12 성능·동시성 (docs/ACCEPTANCE.md): 동시 업로드 세션의 쿼터 예약, 워커 정지 중 수신 계속, T16-b 처리량 보고.

부하 수치(T12-b)는 loadtest/로 운영 하드웨어에서 잰다 — 여기서는 합격 수치를 넣지 않는다 (tech.md HW-8).
"""

from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app.db.models import Asset, OrgQuota
from app.seed import DEV_A_SENSOR, ORG_A
from tests.integration.test_ingest import manifest, mp4_bytes, upload_all

pytestmark = [pytest.mark.integration, pytest.mark.acceptance]
ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.covers("T12-a")
def test_t12a_concurrent_upload_sessions_never_exceed_quota(ictx) -> None:
    ctx = ictx
    size = 1024 * 1024
    with ctx.c.sessionmaker.begin() as db:
        db.merge(OrgQuota(organization_id=ORG_A, quota_bytes=5 * size + 100, used_bytes=0, quarantine_bytes=0,
                          reserved_bytes=0, backup_bytes=0, updated_at=datetime.now(UTC)))
    h = ctx.as_user(102)

    def create(i: int) -> int:
        data = mp4_bytes(size, seed=1000 + i)
        return ctx.client.post("/v1/uploads", json=manifest(data), headers=h).status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        codes = list(pool.map(create, range(12)))
    with ctx.c.sessionmaker() as db:
        q = db.get(OrgQuota, ORG_A)
    assert codes.count(201) == 5 and set(codes) <= {201, 409}, codes  # 나머지는 QUOTA_EXCEEDED
    assert q.reserved_bytes + q.used_bytes <= q.quota_bytes


@pytest.mark.covers("T12-c")
def test_t12c_ingest_continues_while_worker_stopped(ictx) -> None:
    """워커가 없어도(이 시험은 워커를 돌리지 않는다) 업로드 완료·관측값 수신은 되고 처리만 대기열에 쌓인다."""
    ctx = ictx
    h = ctx.as_user(102)
    data = mp4_bytes(2 * 1024 * 1024, seed=77)
    created = upload_all(ctx, manifest(data), data, h)
    now = datetime.now(UTC).replace(microsecond=0)
    r = ctx.client.post("/v1/observations", headers=h, json={
        "schema_version": "1.0.0", "source_system_id": DEV_A_SENSOR, "observations": [
            {"metric": "sea_water_temperature", "value": 17.5, "unit": "degC", "quality_flag": "GOOD",
             "observed_at": (now - timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")} for i in range(5)]})
    assert r.status_code in (200, 207) and r.json()["accepted"] == 5
    with ctx.c.sessionmaker() as db:
        a = db.execute(select(Asset).where(Asset.asset_id == created["asset_id"])).scalar_one()
        assert a.ingest_status == "RECEIVED"  # 처리(무결성 검증)는 워커가 돌아오면
        assert db.execute(text("SELECT count(*) FROM jobs WHERE status='QUEUED' AND job_type='integrity_verify'")) \
            .scalar() >= 1


@pytest.mark.covers("T16-b")
def test_t16b_bench_reports_per_step_throughput(tmp_path: Path) -> None:
    import shutil

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg가 필요합니다")
    out = tmp_path / "bench.json"
    r = subprocess.run([sys.executable, "-m", "app.bench", "--seconds", "1", "--size", "320x240", "--out", str(out)],
                       cwd=ROOT / "backend", capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    steps = json.loads(out.read_text())["steps"]
    assert {"META", "PROXY_CPU", "THUMBNAIL", "DEID_BLUR_CPU"} <= set(steps)
    assert all(s["ok"] and s["frames_per_s"] for s in steps.values())


@pytest.mark.covers("T05-a", "T05-e")
def test_t05_contract_examples_valid_and_invalid() -> None:
    """정상 예시는 모두 통과, contracts/examples/invalid/는 모두 거부 (make contracts-check와 같은 검사)."""
    r = subprocess.run([sys.executable, str(ROOT / "tools/validate_contracts.py")], capture_output=True, text=True,
                       check=False)
    assert r.returncode == 0 and "모든 계약 검사 통과" in r.stdout, r.stdout + r.stderr
