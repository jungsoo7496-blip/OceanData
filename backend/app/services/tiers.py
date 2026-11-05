"""Hot/Warm 계층 위치 (08번 요구사항 9.1·9.3, ADR-0036).

- 계층마다 저장소(StoragePort)와 존별 버킷이 있다. HOT은 S3_*·BUCKET_*, WARM은 WARM_S3_*·WARM_BUCKET_* 설정이다.
  WARM 주소·자격증명을 비우면 HOT과 같은 저장소를 함께 쓰고 버킷만 다르다.
- 자산 객체를 읽는 모든 곳은 `locate()`로 (저장소, 버킷)을 정한다. 위치는 자산 행의 (storage_tier, storage_zone,
  storage_key)이고, Tier_Mover는 사본의 sha256을 확인한 뒤에만 storage_tier를 바꾸므로 이동 중에는 원래 위치를 읽는다.
- 새 객체(업로드·파생본·보고서·데이터셋 사본)는 언제나 HOT에 쓴다. 격리 존은 HOT에만 있다.
- 객체 복사는 GET 스트림 → PUT/멀티파트다 (CopyObject·제품 전용 계층화 기능은 쓰지 않는다, HW-1).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import IO, TYPE_CHECKING, Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.db.models import Asset
from app.ports.storage import TIERS, StoragePort

if TYPE_CHECKING:
    from app.container import Container

log = logging.getLogger(__name__)
PROBE_PREFIX = "_tier_probe/"


@dataclass(frozen=True)
class Place:
    """한 계층의 한 존: 저장소와 버킷."""

    tier: str
    zone: str
    storage: StoragePort
    bucket: str


def storage_of(c: Container, tier: str) -> StoragePort:
    if tier == "HOT":
        return c.storage
    if tier == "WARM":
        return c.warm_storage or c.storage
    raise ValueError(f"알 수 없는 계층: {tier}")


def place(c: Container, tier: str, zone: str) -> Place:
    buckets = c.settings.tier_buckets(tier)
    if zone not in buckets:
        raise ValueError(f"{tier} 계층에는 {zone} 존이 없습니다")
    return Place(tier, zone, storage_of(c, tier), buckets[zone])


def _zone(a: Any) -> str:
    if a.storage_zone:
        return str(a.storage_zone)
    from app.services.ingest import zone_for

    return zone_for(a.variant)


def where(a: Any) -> tuple[str, str, str]:
    """자산 객체의 (계층, 존, 키)."""
    return (getattr(a, "storage_tier", None) or "HOT"), _zone(a), (a.storage_key or "")


def locate(c: Container, a: Any) -> tuple[StoragePort, str]:
    """자산 객체를 읽을 (저장소, 버킷) — 읽는 곳은 모두 이것을 쓴다 (요구사항 9.3)."""
    tier, zone, _ = where(a)
    p = place(c, tier, zone)
    return p.storage, p.bucket


def same_place(a: Place, b: Place) -> bool:
    return a.storage is b.storage and a.bucket == b.bucket


def zone_places(c: Container, zone: str, only: Iterable[str] = TIERS) -> list[Place]:
    """그 존이 있는 모든 계층의 위치 (같은 저장소·버킷은 한 번). 삭제는 모든 계층에서 한다."""
    out: list[Place] = []
    for tier in only:
        if zone in c.settings.tier_buckets(tier):
            p = place(c, tier, zone)
            if not any(same_place(p, q) for q in out):
                out.append(p)
    return out


def in_use(c: Container, db: Session) -> tuple[str, ...]:
    """쓰고 있는 계층. WARM은 계층 이동을 켰거나 WARM 자산·진행 중 이동이 있을 때만
    (안 쓰는 WARM 버킷을 재거나 지우러 가지 않는다)."""
    from app.db.models import TierMove

    if c.settings.tiering_enabled:
        return TIERS
    warm = db.execute(select(Asset.asset_id).where(Asset.storage_tier != "HOT", Asset.deleted_at.is_(None))
                      .limit(1)).first()
    moving = db.execute(select(TierMove.move_id).where(TierMove.status.in_(("COPYING", "SWITCHED"))).limit(1)).first()
    return TIERS if warm or moving else ("HOT",)


def all_places(c: Container, zones: Iterable[str] | None = None,
               only: Iterable[str] = TIERS) -> list[Place]:
    """계층·존 위치 목록 (같은 저장소·버킷은 한 번): 버킷 준비·사용량 실측·암호화 확인용."""
    out: list[Place] = []
    for tier in only:
        for zone in c.settings.tier_buckets(tier):
            if zones is not None and zone not in zones:
                continue
            p = place(c, tier, zone)
            if not any(same_place(p, q) for q in out):
                out.append(p)
    return out


def check(c: Container, tier: str) -> bool:
    """그 계층의 버킷이 모두 닿는가 (/readyz 경고)."""
    return storage_of(c, tier).buckets_ok(c.settings.tier_buckets(tier).values())


def distinct(src: Place, dst: Place) -> bool:
    """두 위치가 정말 다른 저장 공간인가. 같은 저장소를 다른 주소로 가리킨 설정 실수면 이동이 하나뿐인 사본을 지우므로,
    대상에 표식 객체를 쓰고 원래 쪽에서 보이는지 확인한다 (PUT·HEAD·DELETE만 쓴다)."""
    if same_place(src, dst):
        return False
    key = PROBE_PREFIX + secrets.token_hex(8)
    dst.storage.put_object(dst.bucket, key, b"tier-probe", "text/plain")
    try:
        return src.storage.head_object(src.bucket, key) is None
    finally:
        dst.storage.delete_object(dst.bucket, key)


def digest(chunks: Iterable[bytes]) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    for chunk in chunks:
        h.update(chunk)
        n += len(chunk)
    return h.hexdigest(), n


def copy_object(src: Place, dst: Place, key: str, content_type: str | None = None) -> tuple[str, int]:
    """원래 객체를 스트림으로 읽어 대상에 쓴다. 읽은 내용의 (sha256, 크기)를 돌려준다 (원본 손상 판단용)."""
    h = hashlib.sha256()
    size = 0

    def tee() -> Iterator[bytes]:
        nonlocal size
        for chunk in src.storage.iter_object(src.bucket, key):
            h.update(chunk)
            size += len(chunk)
            yield chunk

    dst.storage.put_stream(dst.bucket, key, tee(), content_type)
    return h.hexdigest(), size


def relocate(c: Container, a: Asset, *, zone: str, key: str) -> None:
    """자산 객체를 HOT 계층의 (zone, key)로 옮기고 위치를 바꾼다 (격리·재검증 이동).

    같은 저장소면 기존 move_object, 다르면(WARM이 따로 있는 저장소) 스트림 복사 뒤 원래 객체를 지운다.
    실패하면 예외를 그대로 올리고 위치는 바꾸지 않는다 (호출한 쪽이 기존처럼 처리)."""
    tier, src_zone, src_key = where(a)
    src, dst = place(c, tier, src_zone), place(c, "HOT", zone)
    if src.storage is dst.storage:
        src.storage.move_object(src.bucket, src_key, dst.bucket, key)
    else:
        dst.storage.put_stream(dst.bucket, key, src.storage.iter_object(src.bucket, src_key), a.mime_type)
        src.storage.delete_object(src.bucket, src_key)
    a.storage_tier, a.storage_zone, a.storage_key = "HOT", zone, key


def current(c: Container, asset_id: str) -> tuple[str, str, str] | None:
    """DB에 지금 기록된 (계층, 존, 키). 지워졌거나 바이너리가 없으면 None."""
    with c.sessionmaker() as db:
        a = db.get(Asset, asset_id)
        if a is None or a.deleted_at is not None or not a.storage_key:
            return None
        return where(a)


def download(c: Container, a: Any, f: IO[bytes]) -> None:
    """자산 객체를 파일 f에 스트리밍으로 받는다.

    읽는 도중 실패했는데 그사이 계층 이동으로 위치가 바뀌었으면(오래 걸린 읽기 동안 유예가 끝남) 새 위치에서 한 번 다시
    받는다 (요구사항 9.3). 위치가 그대로면 실패를 그대로 올린다."""
    first = where(a)
    try:
        _stream(c, first, f)
        return
    except Exception:
        now_at = current(c, a.asset_id) if getattr(a, "asset_id", None) else None
        if now_at is None or now_at == first:
            raise
        log.info("asset_read_relocated", extra={"asset_id": a.asset_id, "from_tier": first[0], "to_tier": now_at[0]})
    f.seek(0)
    f.truncate()
    _stream(c, now_at, f)


def _stream(c: Container, loc: tuple[str, str, str], f: IO[bytes]) -> None:
    p = place(c, loc[0], loc[1])
    for chunk in p.storage.iter_object(p.bucket, loc[2]):
        f.write(chunk)


TOUCH_COALESCE = timedelta(hours=1)  # 규칙은 일 단위라 재생마다 쓰지 않는다 (쓰기·행 잠금을 줄임)


def touch_accessed(db: Session, asset_id: str, now: datetime) -> None:
    """사용자가 바이너리를 연 시각 (계층 규칙의 '마지막 조회'). 메타데이터 변경이 아니라 updated_at은 그대로 둔다."""
    db.execute(update(Asset).where(Asset.asset_id == asset_id, or_(
        Asset.last_accessed_at.is_(None), Asset.last_accessed_at < now - TOUCH_COALESCE))
        .values(last_accessed_at=now).execution_options(synchronize_session=False))


def assets_by_tier(db: Session, organization_id: str | None = None) -> dict[str, int]:
    """활성 자산(바이너리 있음)의 계층별 크기 합."""
    from sqlalchemy import func

    q = select(Asset.storage_tier, func.sum(Asset.byte_size)).where(
        Asset.deleted_at.is_(None), Asset.has_binary.is_(True), Asset.storage_key.is_not(None))
    if organization_id:
        q = q.where(Asset.organization_id == organization_id)
    got = {t: int(n or 0) for t, n in db.execute(q.group_by(Asset.storage_tier)).all()}
    return {t: got.get(t, 0) for t in TIERS}
