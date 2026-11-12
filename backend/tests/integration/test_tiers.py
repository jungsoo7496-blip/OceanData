"""08번 요구사항 9: Hot/Warm 계층과 Tier_Mover (ADR-0036).

HOT → WARM 이동(사본을 다시 읽어 sha256 확인 → 위치 전환 → 다운로드 URL 유효시간 이상 유예 뒤 원래 객체 삭제),
이동 중·후 조회와 다운로드 URL, 사본·원본 손상, 다시 실행, 삭제·데이터셋·백업이 WARM에서 읽기, 계층별 사용량,
되돌리기(recall), 같은 저장 공간 설정 실수 방지, 마이그레이션 되돌리기 보호. 합성 데이터만 쓴다.
"""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, text

from app.adapters.s3_storage import S3Storage
from app.adapters.system_clock import FixedClock
from app.db.models import Asset, JobRow, TierMove, TierPolicy, UsageSnapshot
from app.domain.ids import new_id
from app.seed import DEV_A_CAMERA, ORG_A, PRJ_A
from app.services import backup, capacity, retention, tier_mover, tiers
from app.workers import main as worker
from tests.acceptance.test_t08_t15_sharing import CARD, approved_pair, share
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_lifecycle import upload_csv

pytestmark = pytest.mark.integration
ADMIN_A, MEMBER_A, OP = 101, 102, 106


def csv(rows: int, seed: int = 0) -> bytes:
    """합성 센서 CSV (실측 데이터 아님)."""
    return b"t,v\n" + b"".join(f"{i},{(i * 7 + seed) % 97}\n".encode() for i in range(rows))


def enable(ctx, *, separate: bool = True, create_buckets: bool = True, **update) -> None:  # type: ignore[no-untyped-def]
    """계층 이동을 켠다. separate면 WARM을 따로 연결한 저장소(같은 S3 서버, 다른 클라이언트)로 둔다."""
    s = ctx.c.settings.model_copy(update={"tiering_enabled": True, "tier_delete_grace_s": 0, **update})
    ctx.c.settings = s  # API 앱도 같은 컨테이너를 쓴다
    ctx.c.warm_storage = S3Storage(s.warm_s3_settings()) if separate else None
    if create_buckets:
        for p in tiers.all_places(ctx.c, only=("WARM",)):
            p.storage.ensure_bucket(p.bucket)


def set_rule(ctx, policy_id: str = "raw_original_to_warm", *, zone: str = "RAW",  # type: ignore[no-untyped-def]
             variant: str | None = "ORIGINAL", age: int = 180, idle: int | None = 90, enabled: bool = True) -> None:
    with ctx.c.sessionmaker.begin() as db:
        db.merge(TierPolicy(policy_id=policy_id, zone=zone, variant=variant, min_age_days=age, min_idle_days=idle,
                            enabled=enabled, updated_at=datetime.now(UTC)))


def backdate(ctx, asset_id: str, *, ingested_days: int, accessed_days: int | None = None) -> None:  # type: ignore[no-untyped-def]
    with ctx.c.engine.begin() as conn:
        conn.execute(text("UPDATE assets SET ingested_at = now() - make_interval(days => CAST(:d AS INTEGER)), "
                          "last_accessed_at = "
                          "CASE WHEN CAST(:x AS INTEGER) IS NULL THEN NULL "
                          "ELSE now() - make_interval(days => CAST(:x AS INTEGER)) END WHERE asset_id = :a"),
                     {"d": ingested_days, "x": accessed_days, "a": asset_id})


def drain(ctx) -> None:  # type: ignore[no-untyped-def]
    while worker.run_once(ctx.c, "t"):
        pass


def move(ctx, move_id: int) -> TierMove:  # type: ignore[no-untyped-def]
    with ctx.c.sessionmaker() as db:
        return db.get(TierMove, move_id)


def asset(ctx, asset_id: str) -> Asset:  # type: ignore[no-untyped-def]
    with ctx.c.sessionmaker() as db:
        return db.get(Asset, asset_id)


def obj(ctx, tier: str, zone: str, key: str) -> bytes | None:  # type: ignore[no-untyped-def]
    p = tiers.place(ctx.c, tier, zone)
    return p.storage.get_object(p.bucket, key) if p.storage.head_object(p.bucket, key) is not None else None


def at(ctx, when: datetime):  # type: ignore[no-untyped-def]
    return dataclasses.replace(ctx.c, clock=FixedClock(when))


def url_for(ctx, asset_id: str) -> str:  # type: ignore[no-untyped-def]
    r = ctx.client.post(f"/v1/assets/{asset_id}/download-url", headers=ctx.as_user(MEMBER_A))
    check(r, "POST", "/v1/assets/{asset_id}/download-url")
    return str(r.json()["url"])


def tier_jobs(ctx) -> list[tuple]:  # type: ignore[no-untyped-def]
    with ctx.c.engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(
            "SELECT status, attempts, last_error_code FROM jobs WHERE job_type = 'tier_move' ORDER BY created_at"))]


def test_hot_to_warm_verified_move_keeps_reads_and_urls(ictx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ctx = ictx
    data = csv(400)
    aid = upload_csv(ctx, data)
    a = asset(ctx, aid)
    key, hot_bucket = a.storage_key, ctx.c.settings.bucket_raw
    assert (a.storage_tier, a.storage_zone, a.ingest_status) == ("HOT", "RAW", "READY_PRIVATE")
    enable(ctx)
    warm_bucket = ctx.c.settings.tier_buckets("WARM")["RAW"]

    # 규칙 초안은 꺼져 있다 (값은 운영기관 확정 전) → 오래된 자산이어도 옮기지 않는다
    backdate(ctx, aid, ingested_days=200)
    assert tier_mover.schedule(ctx.c) == []
    set_rule(ctx)  # RAW·ORIGINAL, 들어온 뒤 180일, 마지막 조회 뒤 90일
    backdate(ctx, aid, ingested_days=100)  # 경과 일수 부족
    assert tier_mover.schedule(ctx.c) == []
    backdate(ctx, aid, ingested_days=200, accessed_days=10)  # 최근에 열었다
    assert tier_mover.schedule(ctx.c) == []
    backdate(ctx, aid, ingested_days=200, accessed_days=120)
    with ctx.c.sessionmaker() as db:  # dry-run (CLI `python -m app.cli tiers`와 같은 함수)
        plan = tier_mover.plan(ctx.c, db, ctx.c.clock.now())
    rule = next(r for r in plan["rules"] if r["policy_id"] == "raw_original_to_warm")
    assert (rule["candidate_count"], rule["candidate_bytes"]) == (1, len(data))
    assert plan["grace_s"] == ctx.c.settings.presign_download_ttl_s and plan["stored_by_tier"]["WARM"] == 0

    # 법적 보류 자산도 옮긴다: 보류는 삭제 금지이고, 원래 객체는 sha256이 같은 사본을 확인한 뒤에만 지운다 (ADR-0036)
    r = ctx.client.post(f"/v1/assets/{aid}/legal-hold", json={"hold": True, "reason": "분쟁 대응 (시험)"},
                        headers=ctx.as_user(OP, mfa=True))
    assert r.status_code == 200, r.text
    [move_id] = tier_mover.schedule(ctx.c)
    assert tier_mover.schedule(ctx.c) == []  # 열린 이동이 있으면 다시 고르지 않는다
    assert move(ctx, move_id).status == "COPYING" and asset(ctx, aid).storage_tier == "HOT"

    # 이동 중(사본 확인 뒤·위치 전환 직전): 자산과 다운로드 URL은 원래 위치, 사본은 이미 확인됨
    seen: dict[str, str] = {}
    real_switch = tier_mover.switch

    def during(c, mid):  # type: ignore[no-untyped-def]
        assert asset(ctx, aid).storage_tier == "HOT"
        assert obj(ctx, "WARM", "RAW", key) == data
        seen["url"] = url_for(ctx, aid)
        assert f"/{hot_bucket}/" in seen["url"]
        assert httpx.get(seen["url"], timeout=30).content == data
        return real_switch(c, mid)

    monkeypatch.setattr(tier_mover, "switch", during)
    drain(ctx)
    monkeypatch.setattr(tier_mover, "switch", real_switch)
    assert seen and tier_jobs(ctx) == [("DONE", 1, None)]
    mv = move(ctx, move_id)
    assert (mv.status, mv.from_tier, mv.to_tier, mv.legal_hold) == ("SWITCHED", "HOT", "WARM", True)
    # 유예 = max(TIER_DELETE_GRACE_S=0, PRESIGN_DOWNLOAD_TTL_S): 전환 직전에 발급한 URL이 끝까지 쓰인다
    assert mv.delete_after - mv.switched_at == timedelta(seconds=ctx.c.settings.presign_download_ttl_s)
    a = asset(ctx, aid)
    assert (a.storage_tier, a.legal_hold, a.checksum_sha256) == ("WARM", True, hashlib.sha256(data).hexdigest())
    assert httpx.get(seen["url"], timeout=30).content == data  # 원래 위치 URL (유예 중)
    new_url = url_for(ctx, aid)
    assert f"/{warm_bucket}/" in new_url and httpx.get(new_url, timeout=30).content == data
    assert asset(ctx, aid).last_accessed_at is not None  # 다운로드 URL 발급이 '마지막 조회'

    # 유예 동안 두 벌: 일일 대조는 잠시 두 벌인 객체를 빼고 본다, 유예 전에는 지우지 않는다
    with ctx.c.sessionmaker.begin() as db:
        assert [d for d in retention.reconcile(ctx.c, db) if d["organization_id"] == ORG_A] == []
    assert tier_mover.finish_due(ctx.c)["done"] == 0 and obj(ctx, "HOT", "RAW", key) == data

    # 유예가 지나면 원래 객체를 지운다
    assert tier_mover.finish_due(at(ctx, mv.delete_after + timedelta(seconds=1)))["done"] == 1
    assert move(ctx, move_id).status == "DONE"
    assert obj(ctx, "HOT", "RAW", key) is None and obj(ctx, "WARM", "RAW", key) == data
    assert httpx.get(seen["url"], timeout=30).status_code == 404  # (실제로는 이미 만료된 URL)
    assert httpx.get(url_for(ctx, aid), timeout=30).content == data
    # 다시 실행해도 그대로 (멱등)
    assert tier_mover.run_move(ctx.c, move_id) == "DONE" and tier_mover.schedule(ctx.c) == []

    # WARM에서 읽기: 처리 작업의 임시 사본·무결성 표본 검증·센서 재파싱
    from app.services.integrity import verify_integrity
    from app.services.pipeline import local_copy, sensor_parse

    with ctx.c.sessionmaker() as db:
        a = db.get(Asset, aid)
        db.expunge(a)
    with local_copy(ctx.c, a) as path, open(path, "rb") as f:
        assert f.read() == data
    rep = verify_integrity(ctx.c, sample_size=50)
    assert aid not in rep.missing_objects and aid not in rep.mismatched_assets
    assert sensor_parse(ctx.c, aid) == "OK"

    # 계층별 사용량 (요구사항 10.1): 스냅숏·월간 리포트, 일일 대조는 WARM 버킷까지 잰다
    today = datetime.now(UTC).date()
    with ctx.c.sessionmaker.begin() as db:
        capacity.snapshot(ctx.c, db, today)
    with ctx.c.sessionmaker() as db:
        snap = db.get(UsageSnapshot, (today, ORG_A))
        assert snap.by_tier == {"HOT": 0, "WARM": len(data)} and snap.by_zone == {"RAW": len(data)}
    r = ctx.client.get("/v1/usage-reports", params={"month": today.strftime("%Y-%m")}, headers=ctx.as_user(ADMIN_A))
    check(r, "GET", "/v1/usage-reports")
    assert r.json()["items"][0]["storage_by_tier"] == {"HOT": 0, "WARM": len(data)}
    with ctx.c.sessionmaker.begin() as db:
        assert [d for d in retention.reconcile(ctx.c, db) if d["organization_id"] == ORG_A] == []
    # 법적 보류는 그대로다: 삭제 요청은 거부된다 (요구사항 4.1)
    r = ctx.client.post(f"/v1/assets/{aid}/deletion", json={"reason": "정리"}, headers=ctx.as_user(ADMIN_A, mfa=True))
    assert r.status_code == 409 and obj(ctx, "WARM", "RAW", key) == data


def test_corrupted_copy_or_damaged_source_keeps_original(ictx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ctx = ictx
    data = csv(300, seed=1)
    aid = upload_csv(ctx, data)
    key = asset(ctx, aid).storage_key
    enable(ctx)
    set_rule(ctx)
    backdate(ctx, aid, ingested_days=200)

    # 사본이 깨져 저장된다 (WARM 저장소 문제를 흉내) → 원래 객체·위치 그대로, 이동 FAILED, 사본 정리, 재시도 없음
    warm = ctx.c.warm_storage
    real_put = warm.put_stream

    def corrupt(bucket, k, chunks, content_type=None):  # type: ignore[no-untyped-def]
        body = b"".join(chunks)
        return real_put(bucket, k, [body[:-1] + b"#"], content_type)

    monkeypatch.setattr(warm, "put_stream", corrupt)
    [m1] = tier_mover.schedule(ctx.c)
    drain(ctx)
    mv = move(ctx, m1)
    assert (mv.status, mv.error_code) == ("FAILED", "COPY_CHECKSUM_MISMATCH")
    assert asset(ctx, aid).storage_tier == "HOT" and obj(ctx, "HOT", "RAW", key) == data
    assert obj(ctx, "WARM", "RAW", key) is None
    assert tier_jobs(ctx) == [("FAILED", 1, "COPY_CHECKSUM_MISMATCH")]  # 다시 해도 같은 결과면 바로 실패
    assert httpx.get(url_for(ctx, aid), timeout=30).content == data
    assert tier_mover.schedule(ctx.c) == []  # 실패 뒤 재시도 간격 동안 다시 고르지 않는다

    # 저장소가 돌아오고 재시도 간격이 지나면 다시 옮긴다 (방금 URL을 발급해 '최근 조회'라 그것도 지나야 한다)
    monkeypatch.setattr(warm, "put_stream", real_put)
    later = at(ctx, datetime.now(UTC) + timedelta(seconds=ctx.c.settings.tier_failed_retry_after_s + 60))
    assert tier_mover.schedule(later) == []
    backdate(ctx, aid, ingested_days=200, accessed_days=91)
    [m2] = tier_mover.schedule(later)
    drain(ctx)
    assert move(ctx, m2).status == "SWITCHED" and asset(ctx, aid).storage_tier == "WARM"

    # 원래 객체가 기록과 다르다(손상) → 옮기지 않고 운영자에게 알리며 다시 고르지 않는다
    other = csv(120, seed=2)
    bid = upload_csv(ctx, other)
    bkey = asset(ctx, bid).storage_key
    backdate(ctx, bid, ingested_days=200)
    ctx.c.storage.put_object(ctx.c.settings.bucket_raw, bkey, other[:-2] + b"X\n")  # 같은 크기, 다른 내용
    [m3] = tier_mover.schedule(ctx.c)
    drain(ctx)
    mv = move(ctx, m3)
    assert (mv.status, mv.error_code) == ("FAILED", "SOURCE_CHECKSUM_MISMATCH")
    assert asset(ctx, bid).storage_tier == "HOT" and obj(ctx, "WARM", "RAW", bkey) is None
    assert obj(ctx, "HOT", "RAW", bkey) is not None  # 원래 객체는 건드리지 않는다
    note = next(n for n in ctx.c.notifier.sent if n.kind == "tier.source_problem")
    assert note.recipients and note.data["asset_id"] == bid
    assert tier_mover.schedule(at(ctx, datetime.now(UTC) + timedelta(days=30))) == []


def test_delete_dataset_and_backup_read_from_warm(ictx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ctx = ictx
    a = approved_pair(ctx, ORG_A, PRJ_A, DEV_A_CAMERA, ["AI_TRAINING"])
    deid = asset(ctx, a["deid"])
    want = deid.checksum_sha256
    enable(ctx, backup_bucket=f"{ctx.c.settings.bucket_raw[:-4]}-backup")
    with ctx.c.sessionmaker() as db:  # 백업 회차는 이동 전에 대상을 정했다 (계층: HOT)
        items = [i for i in backup.plan(ctx.c, db) if i["asset_id"] == a["deid"]]
    assert [i["tier"] for i in items] == ["HOT"]

    set_rule(ctx, "deid_to_warm", zone="PROCESSED", variant="DEID", age=0, idle=None)
    assert len(tier_mover.schedule(ctx.c)) == 1
    drain(ctx)
    [mv] = [m for m in _moves(ctx) if m.asset_id == a["deid"]]
    tier_mover.finish_due(at(ctx, mv.delete_after + timedelta(seconds=1)))
    assert obj(ctx, "HOT", "PROCESSED", deid.storage_key) is None
    assert asset(ctx, a["deid"]).storage_tier == "WARM"

    # 백업: 계획 뒤 객체가 WARM으로 옮겨져도 지금 위치에서 읽어 sha256을 확인한다
    dst = ctx.c.storage
    dst.ensure_bucket(ctx.c.settings.backup_bucket)
    objects, failed, _ = backup._copy_objects(ctx.c, dst, items, {})
    assert failed == [] and [o["sha256"] for o in objects] == [want]

    # 데이터셋 버전 사본은 WARM 원본에서 만들고 HOT DATASET 존에 둔다 (요구사항 9.3)
    assert share(ctx, ADMIN_A, a["deid"], "SANDBOX_PARTNERS").json()["state"] == "APPLIED"
    ds = ctx.client.post("/v1/datasets", json={"title": "WARM 원본 (합성)"}, headers=ctx.as_user(OP)).json()
    r = ctx.client.post(f"/v1/datasets/{ds['dataset_id']}/versions", headers=ctx.as_user(OP),
                        json={"version": "1.0.0", "data_card": CARD, "item_asset_ids": [a["deid"]]})
    assert r.status_code == 201, r.text
    item = r.json()["manifest"]["items"][0]
    copied = ctx.c.storage.get_object(ctx.c.settings.bucket_datasets,
                                      f"{ds['dataset_id']}/{r.json()['dataset_version_id']}/{item['path']}")
    assert hashlib.sha256(copied).hexdigest() == item["checksum_sha256"] == want

    # 삭제: 유예 중(원래 객체와 사본 두 벌)인 자산을 지우면 두 계층 모두에서 지운다
    data = csv(50, seed=3)
    cid = upload_csv(ctx, data)
    ckey = asset(ctx, cid).storage_key
    set_rule(ctx, age=0, idle=None)
    [m] = tier_mover.schedule(ctx.c)
    drain(ctx)
    assert move(ctx, m).status == "SWITCHED" and obj(ctx, "HOT", "RAW", ckey) == data == obj(ctx, "WARM", "RAW", ckey)
    r = ctx.client.post(f"/v1/assets/{cid}/deletion", json={"reason": "기업 요청 (시험)"},
                        headers=ctx.as_user(ADMIN_A, mfa=True))
    check(r, "POST", "/v1/assets/{asset_id}/deletion")
    assert r.json()["objects"][0]["object_key"] == ckey
    assert obj(ctx, "HOT", "RAW", ckey) is None and obj(ctx, "WARM", "RAW", ckey) is None
    assert tier_mover.finish_due(at(ctx, datetime.now(UTC) + timedelta(hours=2)))["done"] == 1
    assert move(ctx, m).status == "DONE"

    # 복사 중에 지워진 자산: 위치를 바꾸지 않고 취소, 대상 사본도 남기지 않는다
    eid = upload_csv(ctx, csv(60, seed=4))
    ekey = asset(ctx, eid).storage_key
    [m4] = tier_mover.schedule(ctx.c)
    real_switch = tier_mover.switch

    def delete_first(c, mid):  # type: ignore[no-untyped-def]
        r = ctx.client.post(f"/v1/assets/{eid}/deletion", json={"reason": "기업 요청 (시험)"},
                            headers=ctx.as_user(ADMIN_A, mfa=True))
        assert r.status_code == 200, r.text
        return real_switch(c, mid)

    monkeypatch.setattr(tier_mover, "switch", delete_first)
    drain(ctx)
    monkeypatch.setattr(tier_mover, "switch", real_switch)
    assert (move(ctx, m4).status, move(ctx, m4).error_code) == ("CANCELLED", "ASSET_DELETED")
    assert obj(ctx, "HOT", "RAW", ekey) is None and obj(ctx, "WARM", "RAW", ekey) is None


def _moves(ctx) -> list[TierMove]:  # type: ignore[no-untyped-def]
    with ctx.c.sessionmaker() as db:
        return list(db.execute(select(TierMove).order_by(TierMove.move_id)).scalars())


def test_recall_resume_and_postponed_delete_on_shared_storage(ictx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """WARM이 HOT과 같은 저장소(버킷만 다름)를 쓰는 설정. 되돌리기·멈춘 이동 재등록·읽는 작업이 있으면 유예 삭제 미룸."""
    ctx = ictx
    data = csv(80, seed=5)
    aid = upload_csv(ctx, data)
    key = asset(ctx, aid).storage_key
    enable(ctx, separate=False)
    assert tiers.storage_of(ctx.c, "WARM") is ctx.c.storage
    set_rule(ctx, age=0, idle=None)
    [m1] = tier_mover.schedule(ctx.c)
    drain(ctx)
    mv = move(ctx, m1)
    assert mv.status == "SWITCHED"

    # 전환 전에 시작한 처리 작업이 아직 돌면 원래 객체 삭제를 미룬다
    with ctx.c.sessionmaker.begin() as db:
        db.add(JobRow(job_id=new_id("job"), job_type="make_thumbnail", payload={"asset_id": aid}, status="RUNNING",
                      priority=0, attempts=1, max_attempts=3, run_after=mv.switched_at - timedelta(minutes=5),
                      locked_at=mv.switched_at - timedelta(minutes=1), asset_id=aid, organization_id=ORG_A,
                      created_at=mv.switched_at, updated_at=mv.switched_at))
    after = at(ctx, mv.delete_after + timedelta(seconds=1))
    assert tier_mover.finish_due(after) == {"done": 0, "postponed": 1, "resumed": 0}
    assert obj(ctx, "HOT", "RAW", key) == data
    with ctx.c.engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET status = 'DONE' WHERE job_type = 'make_thumbnail'"))
    assert tier_mover.finish_due(after)["done"] == 1 and obj(ctx, "HOT", "RAW", key) is None

    # 되돌리기 (운영자, 마이그레이션 되돌리기 전): 정책·TIERING_ENABLED와 무관하게 HOT으로
    ctx.c.settings = ctx.c.settings.model_copy(update={"tiering_enabled": False})
    [m2] = tier_mover.recall(ctx.c)
    assert tier_mover.recall(ctx.c) == []  # 열린 이동이 있으면 다시 만들지 않는다
    drain(ctx)
    mv2 = move(ctx, m2)
    assert (mv2.status, mv2.from_tier, mv2.to_tier, mv2.policy_id) == ("SWITCHED", "WARM", "HOT", None)
    assert asset(ctx, aid).storage_tier == "HOT" and httpx.get(url_for(ctx, aid), timeout=30).content == data
    tier_mover.finish_due(at(ctx, mv2.delete_after + timedelta(seconds=1)))
    assert obj(ctx, "WARM", "RAW", key) is None and obj(ctx, "HOT", "RAW", key) == data

    # 정책 이동은 TIERING_ENABLED를 끄면 취소된다 (사본을 남기지 않는다)
    ctx.c.settings = ctx.c.settings.model_copy(update={"tiering_enabled": True})
    [m3] = tier_mover.schedule(ctx.c)
    ctx.c.settings = ctx.c.settings.model_copy(update={"tiering_enabled": False})
    drain(ctx)
    assert (move(ctx, m3).status, move(ctx, m3).error_code) == ("CANCELLED", "TIERING_DISABLED")
    assert asset(ctx, aid).storage_tier == "HOT" and obj(ctx, "WARM", "RAW", key) is None

    # 등록 뒤 작업을 넣지 못한 이동(프로세스 중단)은 정비 주기가 작업을 다시 넣는다.
    # 사본을 다 쓴 뒤 멈췄다면 다시 실행할 때 사본을 다시 읽어 확인만 하고 복사하지 않는다 (멱등)
    ctx.c.settings = ctx.c.settings.model_copy(update={"tiering_enabled": True})
    with ctx.c.sessionmaker.begin() as db:
        a = db.get(Asset, aid)
        lost = tier_mover._open(db, a, "WARM", "raw_original_to_warm", datetime.now(UTC))
    assert lost is not None
    tiers.copy_object(tiers.place(ctx.c, "HOT", "RAW"), tiers.place(ctx.c, "WARM", "RAW"), key)
    monkeypatch.setattr(tiers, "copy_object", lambda *a, **k: pytest.fail("확인된 사본을 다시 복사했다"))
    out = tier_mover.finish_due(at(ctx, datetime.now(UTC) + tier_mover.LOST_AFTER + timedelta(minutes=1)))
    assert out["resumed"] == 1
    drain(ctx)
    assert move(ctx, lost[0]).status == "SWITCHED" and asset(ctx, aid).storage_tier == "WARM"
    assert obj(ctx, "WARM", "RAW", key) == data


def test_same_storage_under_another_address_is_refused(ictx) -> None:  # type: ignore[no-untyped-def]
    """설정 실수: WARM 주소가 같은 저장소의 다른 이름이고 버킷도 같다 → 이동이 하나뿐인 사본을 지우게 되므로 막는다."""
    ctx = ictx
    hot_url = ctx.c.settings.s3_endpoint_url
    if "127.0.0.1" not in hot_url:
        pytest.skip("moto 시험 서버 주소에서만 다른 이름을 만들 수 있습니다")
    data = csv(40, seed=6)
    aid = upload_csv(ctx, data)
    key = asset(ctx, aid).storage_key
    enable(ctx, create_buckets=False, warm_s3_endpoint_url=hot_url.replace("127.0.0.1", "localhost"),
           warm_bucket_raw=ctx.c.settings.bucket_raw)
    assert not ctx.c.settings.warm_shares_hot_storage and ctx.c.warm_storage is not None
    set_rule(ctx, age=0, idle=None)
    assert tier_mover.schedule(ctx.c) == []  # 표식 객체로 같은 공간임을 알아채고 고르지 않는다
    with ctx.c.sessionmaker.begin() as db:  # 그래도 이동이 만들어졌다면 작업이 멈춘다
        forced = tier_mover._open(db, db.get(Asset, aid), "WARM", "raw_original_to_warm", datetime.now(UTC))
    assert forced is not None
    tier_mover._enqueue(ctx.c, *forced)
    drain(ctx)
    assert (move(ctx, forced[0]).status, move(ctx, forced[0]).error_code) == ("FAILED", "TIER_SAME_LOCATION")
    assert asset(ctx, aid).storage_tier == "HOT" and obj(ctx, "HOT", "RAW", key) == data


def test_readyz_warns_when_warm_unreachable(ictx) -> None:  # type: ignore[no-untyped-def]
    ctx = ictx
    enable(ctx, create_buckets=False)  # WARM 버킷이 아직 없다
    r = ctx.client.get("/readyz")
    check(r, "GET", "/readyz")
    assert r.status_code == 200 and "storage_tier_unavailable:WARM" in r.json().get("warnings", [])
    for p in tiers.all_places(ctx.c, only=("WARM",)):
        p.storage.ensure_bucket(p.bucket)
    r = ctx.client.get("/readyz")
    assert r.status_code == 200 and "storage_tier_unavailable:WARM" not in r.json().get("warnings", [])


def test_migration_downgrade_refuses_warm_assets(db_url) -> None:  # type: ignore[no-untyped-def]
    """0022 down: WARM 자산·진행 중 이동이 있으면 멈춘다 (되돌린 코드는 계층을 모른다). HOT으로 되돌린 뒤에는 통과."""
    import os

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.seed import seed
    from tests.integration.test_catalog import _asset

    eng = create_engine(db_url)
    with sessionmaker(eng).begin() as db:
        seed(db)
        x = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
        x.storage_tier, x.storage_zone, x.storage_key = "WARM", "RAW", "org/k"
        db.add(x)
        aid = x.asset_id
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "..", "app/db/migrations"))
    with pytest.raises(RuntimeError, match="WARM 자산 1건"):
        command.downgrade(cfg, "0021")
    with eng.begin() as conn:
        conn.execute(text("UPDATE assets SET storage_tier = 'HOT' WHERE asset_id = :a"), {"a": aid})
    command.downgrade(cfg, "0021")
    with eng.connect() as conn:
        cols = {r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'assets'"))}
        assert "storage_tier" not in cols and "last_accessed_at" not in cols
    command.upgrade(cfg, "head")
    eng.dispose()
