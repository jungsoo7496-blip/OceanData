"""08번 요구사항 8(용량 예측)·10(기관별 월간 자원 사용 리포트)."""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import UTC, date, datetime, timedelta

import pytest

from app.adapters.system_clock import FixedClock
from app.db.models import JobRow, OrgQuota, UsageSnapshot
from app.domain.ids import new_id
from app.seed import DEV_A_CAMERA, ORG_A, ORG_B, PRJ_A
from app.services import capacity
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_catalog import T0, _asset

pytestmark = pytest.mark.integration
ADMIN_A, ADMIN_B, MEMBER_A, OP = 101, 103, 102, 106
TODAY = date(2026, 11, 20)
GB = 1024 ** 3


def _quota(db, org: str, used: int, backup: int = 0) -> None:  # type: ignore[no-untyped-def]
    q = db.get(OrgQuota, org) or OrgQuota(organization_id=org, quota_bytes=10 ** 13, used_bytes=0, quarantine_bytes=0,
                                          reserved_bytes=0, backup_bytes=0, updated_at=T0)
    q.used_bytes, q.backup_bytes = used, backup
    db.merge(q)


def test_capacity_forecast_and_lead_time_alert(ctx) -> None:
    # 10일 동안 하루 10GB씩 늘어난 스냅숏 (A 기관 + B 기관 고정 50GB)
    with ctx.c.sessionmaker.begin() as db:
        for i in range(10):
            d = TODAY - timedelta(days=9 - i)
            for org, total in ((ORG_A, (100 + 10 * i) * GB), (ORG_B, 50 * GB)):
                db.add(UsageSnapshot(day=d, organization_id=org, stored_bytes=total, quarantine_bytes=0,
                                     reserved_bytes=0, backup_bytes=0, by_zone={}, backup_target_bytes=0,
                                     created_at=T0))
    c = dataclasses.replace(ctx.c, clock=FixedClock(datetime(2026, 11, 20, 12, tzinfo=UTC)))
    with c.sessionmaker() as db:  # 용량 미설정: 예상일 없이 '확인 필요'
        f = capacity.forecast(c, db, TODAY)
    assert f["capacity_bytes"] is None and f["reach_date"] is None and "확인 필요" in f["note"]
    assert abs(f["slope_bytes_per_day"] - 10 * GB) < 1 and f["current_bytes"] == 240 * GB

    # 용량 1TB, 70% = 716.8GB, 현재 240GB, 하루 10GB → 48일 뒤 (리드타임 180일 안 → 알림)
    c2 = dataclasses.replace(c, settings=c.settings.model_copy(update={"storage_capacity_bytes": 1024 * GB}))
    with c2.sessionmaker() as db:
        f = capacity.forecast(c2, db, TODAY)
        out = capacity.notify_if_near(c2, db, TODAY)
    assert f["reach_date"] == (TODAY + timedelta(days=48)).isoformat() and f["within_lead_time"] is True
    assert out["within_lead_time"] is True
    note = next(n for n in ctx.c.notifier.sent if n.kind == "capacity.forecast")
    assert note.recipients and "증설" in note.body
    # 리드타임 30일이면 알리지 않는다
    c3 = dataclasses.replace(c2, settings=c2.settings.model_copy(update={"capacity_lead_time_days": 30}))
    sent = len(ctx.c.notifier.sent)
    with c3.sessionmaker() as db:
        assert capacity.notify_if_near(c3, db, TODAY)["within_lead_time"] is False
    assert len(ctx.c.notifier.sent) == sent

    # API: 운영자만, 계약 형식
    r = ctx.client.get("/v1/capacity-forecast", headers=ctx.as_user(OP))
    check(r, "GET", "/v1/capacity-forecast")
    assert ctx.client.get("/v1/capacity-forecast", headers=ctx.as_user(ADMIN_A)).status_code == 403


def test_snapshot_and_monthly_usage_report(ctx) -> None:
    month_day = datetime.now(UTC).replace(microsecond=0)  # 감사 로그는 추가 전용이라 이번 달로 시험한다
    month = month_day.strftime("%Y-%m")
    prev = (month_day.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    with ctx.c.sessionmaker.begin() as db:
        _quota(db, ORG_A, used=5_000, backup=1_200)
        _quota(db, ORG_B, used=700)
        raw = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
        raw.storage_zone, raw.byte_size, raw.backup_class = "RAW", 3_000, "PROTECTED"
        deid = _asset(ORG_A, PRJ_A, DEV_A_CAMERA, variant="DEID")
        deid.storage_zone, deid.byte_size = "PROCESSED", 2_000
        for a in (raw, deid):
            a.checksum_sha256 = hashlib.sha256(a.asset_id.encode()).hexdigest()
        db.add_all([raw, deid])
        # 처리 작업: CPU 90초, GPU 30초 (마지막 시도 시작~끝)
        for rc, secs in (("CPU", 90), ("GPU", 30)):
            db.add(JobRow(job_id=new_id("job"), job_type="proxy", payload={}, status="DONE", priority=0, attempts=1,
                          max_attempts=5, run_after=month_day, locked_at=month_day, organization_id=ORG_A,
                          resource_class=rc, created_at=month_day, updated_at=month_day + timedelta(seconds=secs)))
        raw_id = raw.asset_id
    with ctx.c.sessionmaker.begin() as db:
        assert capacity.snapshot(ctx.c, db, month_day.date()) >= 2
    with ctx.c.sessionmaker() as db:
        snap = db.get(UsageSnapshot, (month_day.date(), ORG_A))
        assert snap.by_zone == {"RAW": 3_000, "PROCESSED": 2_000} and snap.backup_target_bytes == 5_000
        assert (snap.stored_bytes, snap.backup_bytes) == (5_000, 1_200)
    # 다운로드 URL 발급 감사 기록 → 다운로드 사용량 (발급 기준)
    ctx.c.audit.record("DOWNLOAD_URL_ISSUED", actor_kind="USER", actor_id="usr_x", organization_id=ORG_A,
                       target_type="Asset", target_id=raw_id)

    r = ctx.client.get("/v1/usage-reports", params={"month": month}, headers=ctx.as_user(ADMIN_A))
    check(r, "GET", "/v1/usage-reports")
    items = r.json()["items"]
    assert [x["organization_id"] for x in items] == [ORG_A]  # 자기 기관만
    a = items[0]
    assert a["storage_by_zone"]["RAW"] == 3_000 and a["storage_by_tier"] == {"HOT": 5_000, "WARM": 0}
    assert (a["backup_target_bytes"], a["requested_backup_bytes"]) == (5_000, 1_200)
    assert a["processing_seconds"] == {"CPU": 90.0, "GPU": 30.0}
    assert a["downloads"] == {"url_count": 1, "url_bytes": 3_000, "distribution_count": 0}
    assert "won" not in r.text and "price" not in r.text  # 금액 없음
    ops = ctx.client.get("/v1/usage-reports", params={"month": month}, headers=ctx.as_user(OP)).json()["items"]
    assert {ORG_A, ORG_B} <= {x["organization_id"] for x in ops}
    empty = ctx.client.get("/v1/usage-reports", params={"month": prev}, headers=ctx.as_user(ADMIN_A)).json()
    assert empty["items"][0]["snapshot_day"] is None and empty["items"][0]["processing_seconds"]["CPU"] == 0
    assert ctx.client.get("/v1/usage-reports", params={"month": "2026-13"}, headers=ctx.as_user(OP)).status_code == 400
    assert ctx.client.get("/v1/usage-reports", params={"month": month},
                          headers=ctx.as_user(MEMBER_A)).status_code == 403
