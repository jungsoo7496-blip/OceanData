"""무결성 로그 앵커·검증 (05번 요구사항 5.2, 5.3)."""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text

from app.container import Container
from app.db.models import Asset, IntegrityAnchor, IntegrityLogEntry
from app.services.ingest import GENESIS, integrity_payload


def anchor_previous_days(c: Container) -> int:
    """어제까지의 날짜별 앵커 해시를 만든다: 그날 마지막 항목의 entry_hash와 건수를 묶은 해시.

    앵커 값은 외부(관리기관 보고서·별도 저장소)에 옮겨 두면 체인 전체를 다시 쓰는 조작을 발견할 수 있다.
    """
    today = c.clock.now().astimezone(UTC).date()
    made = 0
    with c.sessionmaker.begin() as db:
        db.execute(text("SELECT pg_advisory_xact_lock(7202613)"))
        rows = db.execute(text("""
            SELECT (registered_at AT TIME ZONE 'UTC')::date AS day, max(seq) AS last_seq, count(*) AS n
            FROM integrity_log WHERE registered_at < :today
            GROUP BY 1 ORDER BY 1"""), {"today": datetime.combine(today, datetime.min.time(), UTC)}).all()
        days = db.execute(select(IntegrityAnchor.day)).scalars()
        existing = {a.date() if isinstance(a, datetime) else a for a in days}
        for day, last_seq, n in rows:
            if day in existing:
                continue
            last_hash = db.execute(select(IntegrityLogEntry.entry_hash).where(IntegrityLogEntry.seq == last_seq)) \
                .scalar_one()
            anchor = hashlib.sha256(f"{day.isoformat()}|{last_seq}|{n}|{last_hash}".encode()).hexdigest()
            db.add(IntegrityAnchor(day=datetime.combine(day, datetime.min.time()), last_seq=int(last_seq),
                                   entry_count=int(n), anchor_hash=anchor, created_at=c.clock.now()))
            made += 1
    return made


@dataclass
class IntegrityReport:
    checked_entries: int = 0
    broken_at_seq: int | None = None
    sampled: int = 0
    mismatched_assets: list[str] = field(default_factory=list)
    missing_objects: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.broken_at_seq is None and not self.mismatched_assets and not self.missing_objects


def verify_integrity(c: Container, sample_size: int | None = None, seed: int | None = None) -> IntegrityReport:
    """체인 단절과 표본 객체의 현재 해시 불일치를 보고한다 (요구사항 5.3)."""
    rep = IntegrityReport()
    prev = GENESIS
    with c.sessionmaker() as db:
        for e in db.execute(select(IntegrityLogEntry).order_by(IntegrityLogEntry.seq)).scalars():
            if e.prev_hash != prev or integrity_payload(prev, e.asset_id, e.checksum_sha256, e.byte_size,
                                                        e.registered_at) != e.entry_hash:
                rep.broken_at_seq = e.seq
                break
            prev = e.entry_hash
            rep.checked_entries += 1
        total = db.execute(select(func.count()).select_from(IntegrityLogEntry)).scalar_one()
        n = min(total, sample_size if sample_size is not None else c.settings.integrity_sample_size)
        rng = random.Random(seed)  # noqa: S311 - 표본 추출용 (보안 용도 아님)
        picks = rng.sample(range(total), n) if n else []
        for offset in picks:
            e = db.execute(select(IntegrityLogEntry).order_by(IntegrityLogEntry.seq).offset(offset).limit(1)) \
                .scalar_one()
            a = db.get(Asset, e.asset_id)
            rep.sampled += 1
            if a is None or not a.storage_key or a.storage_zone is None:
                rep.missing_objects.append(e.asset_id)
                continue
            h = hashlib.sha256()
            try:
                for chunk in c.storage.iter_object(c.settings.buckets[a.storage_zone], a.storage_key):
                    h.update(chunk)
            except Exception:  # noqa: BLE001 - 없는 객체도 보고 대상
                rep.missing_objects.append(e.asset_id)
                continue
            if h.hexdigest() != e.checksum_sha256:
                rep.mismatched_assets.append(e.asset_id)
    return rep


def anchors(c: Container, days: int = 30) -> list[dict[str, object]]:
    since = c.clock.now() - timedelta(days=days)
    with c.sessionmaker() as db:
        rows = db.execute(select(IntegrityAnchor).where(IntegrityAnchor.created_at >= since)
                          .order_by(IntegrityAnchor.day)).scalars()
        return [{"day": r.day.date().isoformat(), "last_seq": r.last_seq, "entries": r.entry_count,
                 "anchor_hash": r.anchor_hash} for r in rows]
