"""Tier_Mover — Hot/Warm 계층 이동 (08번 요구사항 9.2~9.4, ADR-0036).

순서 (자산마다 열린 이동(COPYING·SWITCHED)은 하나뿐이다):
1. 고르기: 켜진 정책 규칙(tier_policies: 존·변형·들어온 뒤 일수·마지막 조회 후 일수)에 맞는 HOT 자산으로
   이동(COPYING)을 만들고 작업(tier_move)을 넣는다. 동시에 복사 중인 이동은 TIER_MAX_IN_FLIGHT까지.
   처리 작업이 대기·실행 중인 자산은 미룬다.
2. 복사·확인(작업): 원래 객체를 스트림으로 읽어 대상 계층에 쓰고(GET → PUT/멀티파트), 대상 사본을 끝까지
   다시 읽어 sha256·크기가 자산 기록(checksum_sha256·byte_size)과 같은지 본다. ETag는 믿지 않는다.
   이 동안 자산은 원래 위치를 읽는다.
   - 원래 객체를 읽은 해시가 기록과 다르거나 원래 객체가 없으면 SOURCE_*: 사본을 지우고 FAILED,
     운영자 알림, 다시 고르지 않는다.
   - 사본 해시가 다르면 COPY_CHECKSUM_MISMATCH: 사본을 지우고 FAILED. 원래 객체·위치는 그대로다.
   - 저장소 일시 오류는 작업 재시도, 다 쓰면 FAILED(사본 정리).
3. 전환: 자산 행을 잠그고 그사이 지워지거나 옮겨지지 않았는지 다시 확인한 뒤 storage_tier를 바꾸고 SWITCHED.
   delete_after = 지금 + max(TIER_DELETE_GRACE_S, PRESIGN_DOWNLOAD_TTL_S)
   — 전환 직전에 발급한 다운로드 URL이 끝까지 쓰인다.
4. 유예 삭제(정비 주기): delete_after가 지나면 원래 객체를 지우고 DONE. 전환 전에 시작한 처리 작업이
   아직 돌면 미룬다. 원래 자리가 그사이 다시 자산의 현재 위치가 됐으면 지우지 않는다.
   자산이 지워졌으면 모든 계층에서 지운다.
제품 전용 자동 계층화(수명주기 전환 등)는 쓰지 않는다 (요구사항 9.4, HW-1).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, exists, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import Asset, JobRow, TierMove, TierPolicy, User
from app.metrics import TIER_MOVE_BYTES, TIER_MOVES
from app.ports.notifier import Notification
from app.ports.queue import PermanentJobError
from app.services import tiers
from app.timefmt import iso

if TYPE_CHECKING:
    from app.config import Settings
    from app.container import Container
    from app.ports.queue import Job

log = logging.getLogger(__name__)
JOB_TYPE = "tier_move"
OPEN = ("COPYING", "SWITCHED")
MOVABLE_STATUSES = ("REGISTERED", "PROXIED", "READY_PRIVATE")
POLICY_ZONES = ("RAW", "PROCESSED", "EVIDENCE", "DATASET")  # 격리 존은 옮기지 않는다
NO_RETRY = ("SOURCE_CHECKSUM_MISMATCH", "SOURCE_MISSING")  # 원본 확인이 먼저다 — 정책이 다시 고르지 않는다
LOST_AFTER = timedelta(minutes=10)  # 등록 뒤 작업을 넣지 못한 이동(프로세스 중단)을 다시 넣기까지


def grace(settings: Settings) -> timedelta:
    """원래 객체 삭제 유예: 설정값과 다운로드 URL 유효시간 중 긴 쪽 (요구사항 9.3)."""
    return timedelta(seconds=max(settings.tier_delete_grace_s, settings.presign_download_ttl_s))


def _processing(asset_id: Any) -> Any:
    """그 자산의 처리 작업(계층 이동 제외)이 대기·실행 중."""
    return exists(select(JobRow.job_id).where(JobRow.asset_id == asset_id, JobRow.status.in_(("QUEUED", "RUNNING")),
                                              JobRow.job_type != JOB_TYPE))


def candidates(pol: TierPolicy, now: datetime, retry_after: timedelta) -> Any:
    """규칙에 맞는 HOT 자산 (오래된 것부터)."""
    blocked = exists(select(TierMove.move_id).where(TierMove.asset_id == Asset.asset_id, or_(
        TierMove.status.in_(OPEN),
        and_(TierMove.status == "FAILED", or_(TierMove.error_code.in_(NO_RETRY),
                                              TierMove.updated_at > now - retry_after)))))
    q = select(Asset).where(
        Asset.deleted_at.is_(None), Asset.has_binary.is_(True), Asset.storage_key.is_not(None),
        Asset.storage_tier == "HOT", Asset.storage_zone == pol.zone, Asset.ingest_status.in_(MOVABLE_STATUSES),
        Asset.ingested_at <= now - timedelta(days=pol.min_age_days), ~blocked, ~_processing(Asset.asset_id))
    if pol.variant:
        q = q.where(Asset.variant == pol.variant)
    if pol.min_idle_days is not None:
        q = q.where(func.coalesce(Asset.last_accessed_at, Asset.ingested_at)
                    <= now - timedelta(days=pol.min_idle_days))
    return q.order_by(Asset.ingested_at, Asset.asset_id)


def _open(db: Session, a: Asset, to_tier: str, policy_id: str | None, now: datetime) -> tuple[int, str, str] | None:
    """열린 이동을 만든다. 이미 열린 이동이 있으면(동시에 고름) 만들지 않는다."""
    move_id = db.execute(insert(TierMove).values(
        asset_id=a.asset_id, organization_id=a.organization_id, policy_id=policy_id, from_tier=a.storage_tier,
        to_tier=to_tier, zone=a.storage_zone, object_key=a.storage_key, byte_size=a.byte_size,
        checksum_sha256=a.checksum_sha256, status="COPYING", created_at=now, updated_at=now,
    ).on_conflict_do_nothing(index_elements=["asset_id"], index_where=text("status IN ('COPYING', 'SWITCHED')"))
        .returning(TierMove.move_id)).scalar_one_or_none()
    return (int(move_id), a.asset_id, a.organization_id) if move_id is not None else None


def _enqueue(c: Container, move_id: int, asset_id: str, organization_id: str) -> str:
    return c.queue.enqueue(JOB_TYPE, {"move_id": move_id, "asset_id": asset_id}, organization_id=organization_id)


def schedule(c: Container) -> list[int]:
    """정책 규칙으로 HOT → WARM 이동을 고른다 (정비 주기마다, 여러 워커여도 한 번에 하나)."""
    if not c.settings.tiering_enabled:
        return []
    now = c.clock.now()
    made: list[tuple[int, str, str]] = []
    with c.sessionmaker.begin() as db:
        if not db.execute(text("SELECT pg_try_advisory_xact_lock(hashtext('tier_schedule'))")).scalar():
            return []
        room = c.settings.tier_max_in_flight - int(db.execute(
            select(func.count()).select_from(TierMove).where(TierMove.status == "COPYING")).scalar_one())
        retry_after = timedelta(seconds=c.settings.tier_failed_retry_after_s)
        for pol in db.execute(select(TierPolicy).where(TierPolicy.enabled.is_(True))
                              .order_by(TierPolicy.policy_id)).scalars():
            if room - len(made) <= 0:
                break
            if pol.zone not in POLICY_ZONES:
                log.warning("tier_policy_invalid_zone", extra={"policy_id": pol.policy_id, "zone": pol.zone})
                continue
            rows = list(db.execute(candidates(pol, now, retry_after).limit(room - len(made))).scalars())
            if not rows:
                continue
            try:  # 같은 저장 공간을 두 계층으로 잘못 설정했으면 고르지 않는다 (하나뿐인 사본을 지우게 된다)
                ok = tiers.distinct(tiers.place(c, "HOT", pol.zone), tiers.place(c, "WARM", pol.zone))
            except Exception as exc:  # noqa: BLE001 - WARM 저장소 장애면 이번 주기는 건너뛴다
                log.warning("tier_storage_unavailable", extra={"zone": pol.zone, "error_type": type(exc).__name__})
                continue
            if not ok:
                log.error("tier_same_location", extra={"zone": pol.zone})
                continue
            for a in rows:
                m = _open(db, a, "WARM", pol.policy_id, now)
                if m is not None:
                    made.append(m)
    for m in made:  # 이동 행을 확정한 뒤 작업을 넣는다 (작업이 행보다 먼저 보이지 않게)
        _enqueue(c, *m)
    return [m[0] for m in made]


def recall(c: Container, limit: int | None = None) -> list[int]:
    """WARM 자산을 HOT으로 되돌리는 이동을 만든다 (운영자: 마이그레이션 되돌리기·WARM 철수).
    정책 규칙·TIERING_ENABLED와 무관하다."""
    now = c.clock.now()
    made: list[tuple[int, str, str]] = []
    with c.sessionmaker.begin() as db:
        q = select(Asset).where(Asset.deleted_at.is_(None), Asset.has_binary.is_(True), Asset.storage_key.is_not(None),
                                Asset.storage_tier == "WARM",
                                ~exists(select(TierMove.move_id).where(TierMove.asset_id == Asset.asset_id,
                                                                       TierMove.status.in_(OPEN))))
        q = q.order_by(Asset.asset_id)
        for a in db.execute(q.limit(limit) if limit else q).scalars():
            m = _open(db, a, "HOT", None, now)
            if m is not None:
                made.append(m)
    for m in made:
        _enqueue(c, *m)
    return [m[0] for m in made]


# ---------------------------------------------------------------- 복사·확인·전환 (작업)

def _changed(a: Asset | None, mv: TierMove) -> str | None:
    if a is None or a.deleted_at is not None or not a.has_binary or not a.storage_key:
        return "ASSET_DELETED"
    if (a.storage_tier, a.storage_zone, a.storage_key) != (mv.from_tier, mv.zone, mv.object_key):
        return "ASSET_RELOCATED"
    if (a.checksum_sha256, a.byte_size) != (mv.checksum_sha256, mv.byte_size):
        return "ASSET_CHANGED"
    return None


def _live(c: Container, a: Asset | None, p: tiers.Place, key: str) -> bool:
    """(p, key)가 지금 자산의 현재 위치(같은 저장소·버킷·키)인가."""
    if a is None or a.deleted_at is not None or not a.storage_key:
        return False
    tier, zone, akey = tiers.where(a)
    return akey == key and tiers.same_place(tiers.place(c, tier, zone), p)


def _drop(c: Container, a: Asset | None, mv: TierMove, tier: str) -> None:
    """이동의 한쪽 객체를 지운다. 자산이 지워졌으면 모든 계층에서 지운다 (삭제와 이동이 겹친 경우 사본까지).

    살아 있는 자산이면 지울 자리가 현재 위치와 물리적으로 같은 공간이 아닌지 표식 객체로 확인한 뒤에만 지운다
    (같은 저장소를 다른 주소로 가리킨 설정에서 하나뿐인 사본을 지우지 않게). 확인하지 못하면 예외 — 지우지 않는다."""
    if a is None or a.deleted_at is not None:
        for p in tiers.zone_places(c, mv.zone):
            p.storage.delete_object(p.bucket, mv.object_key)
        return
    p = tiers.place(c, tier, mv.zone)
    if _live(c, a, p, mv.object_key):
        return
    atier, azone, akey = tiers.where(a)
    if akey == mv.object_key and not tiers.distinct(tiers.place(c, atier, azone), p):
        log.error("tier_same_location", extra={"move_id": mv.move_id, "zone": mv.zone})
        return
    p.storage.delete_object(p.bucket, mv.object_key)


def _close(c: Container, db: Session, mv: TierMove, a: Asset | None, status: str, code: str) -> None:
    """COPYING 이동을 끝낸다 (FAILED·CANCELLED). 대상 사본은 지우고 원래 객체·자산 위치는 그대로 둔다."""
    now = c.clock.now()
    mv.status, mv.error_code, mv.finished_at, mv.updated_at = status, code, now, now
    try:
        _drop(c, a, mv, mv.to_tier)
    except Exception as exc:  # noqa: BLE001 - 남은 사본은 일일 사용량 대조에서 차이로 드러난다
        log.warning("tier_copy_cleanup_failed", extra={"move_id": mv.move_id, "error_type": type(exc).__name__})
    TIER_MOVES.labels(mv.to_tier, status).inc()
    log.warning("tier_move_" + status.lower(), extra={"move_id": mv.move_id, "asset_id": mv.asset_id,
                                                      "error_code": code})


def _notify_source_problem(c: Container, db: Session, mv: TierMove, code: str) -> None:
    to = tuple(sorted({e for e in db.execute(select(User.contact_email).where(
        User.status == "ACTIVE", User.contact_email.is_not(None),
        User.roles.any("PLATFORM_OPERATOR"))).scalars() if e}))  # type: ignore[arg-type]
    c.notifier.send(Notification(
        kind="tier.source_problem", organization_id=None, subject=f"[계층 이동 중단] 원본 확인 필요 {mv.asset_id}",
        body="\n".join([f"자산 {mv.asset_id}의 {mv.from_tier} 객체가 기록과 다릅니다 ({code}).",
                        "이동은 멈췄고 원래 객체는 그대로 두었습니다.",
                        "make verify-integrity로 표본 해시를 확인하세요."]),
        recipients=to, data={"asset_id": mv.asset_id, "move_id": mv.move_id, "error_code": code}))


def fail(c: Container, move_id: int, code: str, *, notify: bool = False) -> None:
    with c.sessionmaker.begin() as db:
        mv = db.get(TierMove, move_id)
        if mv is None:
            return
        a = db.get(Asset, mv.asset_id, with_for_update=True)  # 자산 먼저 잠근다 (전환·유예 삭제와 같은 순서)
        mv = db.get(TierMove, move_id, with_for_update=True, populate_existing=True)
        if mv is None or mv.status != "COPYING":
            return
        _close(c, db, mv, a, "FAILED", code)
        if notify:
            _notify_source_problem(c, db, mv, code)


def _verified(p: tiers.Place, key: str, sha: str, size: int) -> bool:
    """대상 사본을 끝까지 다시 읽어 sha256·크기를 확인한다 (ETag를 믿지 않는다)."""
    head = p.storage.head_object(p.bucket, key)
    if head is None or head.size != size:
        return False
    return tiers.digest(p.storage.iter_object(p.bucket, key)) == (sha, size)


def run_move(c: Container, move_id: int) -> str:
    """작업 처리기: 복사 → sha256 확인 → 전환. 다시 실행해도 같은 결과다 (이미 확인된 사본은 다시 쓰지 않는다)."""
    now = c.clock.now()
    with c.sessionmaker.begin() as db:
        mv = db.get(TierMove, move_id)
        if mv is None or mv.status != "COPYING":
            return mv.status if mv else "SKIPPED"
        a = db.get(Asset, mv.asset_id, with_for_update=True)  # 자산 먼저 잠근다 (전환·유예 삭제·격리 이동과 같은 순서)
        mv = db.get(TierMove, move_id, with_for_update=True, populate_existing=True)
        if mv is None or mv.status != "COPYING":
            return mv.status if mv else "SKIPPED"
        why = _changed(a, mv)
        if why is None and mv.policy_id is not None and not c.settings.tiering_enabled:
            why = "TIERING_DISABLED"  # 운영자가 자동 이동을 껐다 (되돌리기 이동은 계속한다)
        if why is not None:
            _close(c, db, mv, a, "CANCELLED", why)
            return "CANCELLED"
        assert a is not None
        mv.updated_at = now
        src, dst = tiers.place(c, mv.from_tier, mv.zone), tiers.place(c, mv.to_tier, mv.zone)
        key, sha, size, mime, to_tier = mv.object_key, mv.checksum_sha256, mv.byte_size, a.mime_type, mv.to_tier
    if not tiers.distinct(src, dst):  # 같은 저장 공간이면 원래 객체를 지울 때 하나뿐인 사본이 사라진다
        fail(c, move_id, "TIER_SAME_LOCATION")
        raise PermanentJobError("TIER_SAME_LOCATION")
    if src.storage.head_object(src.bucket, key) is None:
        fail(c, move_id, "SOURCE_MISSING", notify=True)
        raise PermanentJobError("SOURCE_MISSING")
    if not _verified(dst, key, sha, size):  # 다시 실행: 이미 확인되는 사본이 있으면 복사를 건너뛴다
        read = tiers.copy_object(src, dst, key, mime)
        if read != (sha, size):  # 원래 객체가 기록과 다르다 — 옮기지 않는다
            fail(c, move_id, "SOURCE_CHECKSUM_MISMATCH", notify=True)
            raise PermanentJobError("SOURCE_CHECKSUM_MISMATCH")
        if not _verified(dst, key, sha, size):
            fail(c, move_id, "COPY_CHECKSUM_MISMATCH")
            raise PermanentJobError("COPY_CHECKSUM_MISMATCH")
        TIER_MOVE_BYTES.labels(to_tier).inc(size)
    return switch(c, move_id)


def switch(c: Container, move_id: int) -> str:
    """확인된 사본으로 자산 위치를 바꾼다. 그사이 자산이 지워지거나 옮겨졌으면 이동을 취소하고 사본을 지운다."""
    now = c.clock.now()
    with c.sessionmaker.begin() as db:
        mv = db.get(TierMove, move_id)
        if mv is None:
            return "SKIPPED"
        a = db.get(Asset, mv.asset_id, with_for_update=True)
        mv = db.get(TierMove, move_id, with_for_update=True, populate_existing=True)
        if mv is None or mv.status != "COPYING":
            return mv.status if mv else "SKIPPED"
        why = _changed(a, mv)
        if why is not None:
            _close(c, db, mv, a, "CANCELLED", why)
            return "CANCELLED"
        assert a is not None
        a.storage_tier = mv.to_tier
        mv.status, mv.legal_hold, mv.switched_at, mv.updated_at = "SWITCHED", a.legal_hold, now, now
        mv.delete_after = now + grace(c.settings)
        to_tier = mv.to_tier
    TIER_MOVES.labels(to_tier, "SWITCHED").inc()
    log.info("tier_switched", extra={"move_id": move_id, "to_tier": to_tier})
    return "SWITCHED"


def handle_job(c: Container, job: Job) -> str:
    return run_move(c, int(job.payload["move_id"]))


def job_exhausted(c: Container, job: Job, code: str) -> None:
    """재시도를 다 쓴 이동 → FAILED (사본 정리, 원래 객체·위치 그대로)."""
    fail(c, int(job.payload["move_id"]), code or "COPY_FAILED")


# ---------------------------------------------------------------- 유예 삭제·멈춘 이동 (정비 주기)

def _reading_since(db: Session, asset_id: str, since: datetime | None) -> bool:
    """전환 전에 시작해 아직 원래 위치를 읽고 있을 수 있는 처리 작업이 있는가."""
    q = select(JobRow.job_id).where(JobRow.asset_id == asset_id, JobRow.status == "RUNNING",
                                    JobRow.job_type != JOB_TYPE)
    if since is not None:
        q = q.where(JobRow.locked_at < since)
    return db.execute(q.limit(1)).first() is not None


def finish_due(c: Container, limit: int = 200) -> dict[str, int]:
    """유예가 지난 원래 객체를 지우고(DONE), 작업을 넣지 못하고 남은 이동은 작업을 다시 넣는다."""
    now = c.clock.now()
    out = {"done": 0, "postponed": 0, "resumed": 0}
    lost: list[tuple[int, str, str]] = []
    with c.sessionmaker.begin() as db:
        if not db.execute(text("SELECT pg_try_advisory_xact_lock(hashtext('tier_finish'))")).scalar():
            return out
        due = db.execute(select(TierMove.move_id, TierMove.asset_id).where(
            TierMove.status == "SWITCHED", TierMove.delete_after <= now)
            .order_by(TierMove.delete_after).limit(limit)).all()
        for move_id, asset_id in due:
            a = db.get(Asset, asset_id, with_for_update=True)
            mv = db.get(TierMove, move_id, with_for_update=True, populate_existing=True)
            if mv is None or mv.status != "SWITCHED":
                continue
            if a is not None and a.deleted_at is None and _reading_since(db, asset_id, mv.switched_at):
                out["postponed"] += 1
                continue
            try:
                _drop(c, a, mv, mv.from_tier)
            except Exception as exc:  # noqa: BLE001 - 다음 주기에 다시
                log.warning("tier_old_copy_delete_failed", extra={"move_id": move_id, "error_type": type(exc).__name__})
                continue
            mv.status, mv.finished_at, mv.updated_at = "DONE", now, now
            TIER_MOVES.labels(mv.to_tier, "DONE").inc()
            out["done"] += 1
        lost = [(int(m), a, o) for m, a, o in db.execute(
            select(TierMove.move_id, TierMove.asset_id, TierMove.organization_id).where(
                TierMove.status == "COPYING", TierMove.updated_at < now - LOST_AFTER,
                ~exists(select(JobRow.job_id).where(JobRow.job_type == JOB_TYPE, JobRow.asset_id == TierMove.asset_id,
                                                    JobRow.status.in_(("QUEUED", "RUNNING")))))).all()]
    for m in lost:
        _enqueue(c, *m)
        out["resumed"] += 1
    if out["done"] or out["resumed"]:
        log.info("tier_finish", extra=out)
    return out


# ---------------------------------------------------------------- 대조·보고

def transient_bytes(c: Container, db: Session, organization_id: str, zones: tuple[str, ...]) -> int:
    """이동 중 사본·유예 중 원래 객체처럼 잠시 두 벌 있는 객체 크기 (일일 사용량 대조에서 뺀다)."""
    total = 0
    for mv in db.execute(select(TierMove).where(TierMove.organization_id == organization_id,
                                                TierMove.status.in_(OPEN), TierMove.zone.in_(zones))).scalars():
        a = db.get(Asset, mv.asset_id)
        for p in tiers.zone_places(c, mv.zone):
            if _live(c, a, p, mv.object_key):
                continue
            info = p.storage.head_object(p.bucket, mv.object_key)
            total += info.size if info is not None else 0
    return total


def plan(c: Container, db: Session, now: datetime) -> dict[str, Any]:
    """dry-run: 규칙별 지금 옮길 수 있는 자산 수·용량, 계층별 저장량, 이동 상태 (운영자 CLI)."""
    retry_after = timedelta(seconds=c.settings.tier_failed_retry_after_s)
    rules = []
    for pol in db.execute(select(TierPolicy).order_by(TierPolicy.policy_id)).scalars():
        row: dict[str, Any] = {"policy_id": pol.policy_id, "zone": pol.zone, "variant": pol.variant,
                               "min_age_days": pol.min_age_days, "min_idle_days": pol.min_idle_days,
                               "enabled": pol.enabled, "candidate_count": 0, "candidate_bytes": 0}
        if pol.zone in POLICY_ZONES:
            n, total = db.execute(candidates(pol, now, retry_after).order_by(None).with_only_columns(
                func.count(), func.coalesce(func.sum(Asset.byte_size), 0))).one()
            row.update(candidate_count=int(n), candidate_bytes=int(total))
        else:
            row["note"] = "옮길 수 없는 존 (RAW·PROCESSED·EVIDENCE·DATASET만)"
        rules.append(row)
    moves = dict(db.execute(select(TierMove.status, func.count()).group_by(TierMove.status)).tuples().all())
    failed = db.execute(select(TierMove).where(TierMove.status == "FAILED")
                        .order_by(TierMove.updated_at.desc()).limit(20)).scalars()
    return {"generated_at": iso(now), "tiering_enabled": c.settings.tiering_enabled,
            "warm_shares_hot_storage": c.settings.warm_shares_hot_storage,
            "grace_s": int(grace(c.settings).total_seconds()), "max_in_flight": c.settings.tier_max_in_flight,
            "rules": rules, "stored_by_tier": tiers.assets_by_tier(db),
            "moves": {k: int(v) for k, v in moves.items()},
            "recent_failures": [{"move_id": m.move_id, "asset_id": m.asset_id, "error_code": m.error_code,
                                 "at": iso(m.updated_at)} for m in failed]}
