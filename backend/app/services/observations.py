"""관측값 수신·조회·정정, 매핑 프로필, 센서 파일 파싱 (07번).

REST 배치, CSV·JSON 파일 파싱, MQTT 플러그인(에이전트가 REST로 보냄) 세 경로가 모두 `ingest_items()`를 거친다.
같은 규칙으로 같은 저장 결과가 나온다 (요구사항 9.2).
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.container import Container
from app.db.models import Device, ObservationCorrection, ObservationMapping
from app.domain.observations import Normalized, Rejected, normalize, same_value
from app.domain.principal import Principal
from app.services import contracts

# 파티션 테이블은 마이그레이션 0002의 SQL로 만들었다. 별도 MetaData라 Alembic 자동 생성 대상이 아니다
_md = MetaData()
OBS = Table(
    "observations", _md,
    Column("organization_id", String(30)), Column("source_system_id", String(30)), Column("metric", String(80)),
    Column("observed_at", DateTime(timezone=True)), Column("depth_m", Float), Column("channel", String(32)),
    Column("value", Float), Column("unit", String(32)), Column("original_value", Float),
    Column("original_unit", String(32)), Column("quality_flag", String(20)), Column("source_timestamp", String(64)),
    Column("clock_offset_ms", Integer), Column("source_asset_id", String(30)), Column("session_id", String(30)),
    Column("received_at", DateTime(timezone=True)),
)
KEY_COLS = ("organization_id", "source_system_id", "metric", "observed_at", "depth_m", "channel")


def metrics_table() -> dict[str, str]:
    return {m["code"]: m["unit"] for m in contracts.code_table("metrics")["items"]}


@dataclass
class BatchResult:
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)

    def add(self, index: int, status: str, reason: str | None = None) -> None:
        r: dict[str, Any] = {"index": index, "status": status}
        if reason:
            r["reason"] = reason
        self.results.append(r)
        if status == "ACCEPTED":
            self.accepted += 1
        elif status == "DUPLICATE":
            self.duplicates += 1
        else:
            self.rejected += 1

    def as_dict(self) -> dict[str, Any]:
        return {"accepted": self.accepted, "duplicates": self.duplicates, "rejected": self.rejected,
                "results": sorted(self.results, key=lambda r: r["index"])}


def ingest_items(c: Container, db: Session, *, organization_id: str, device: Device, items: list[dict[str, Any]],
                 session_id: str | None, clock_offset_ms: int | None, source_asset_id: str | None = None,
                 time_format: str | None = None) -> BatchResult:
    """항목별로 정규화·검증해 저장한다. 같은 키·같은 값은 DUPLICATE, 다른 값은 VALUE_CONFLICT (덮어쓰지 않음)."""
    s = c.settings
    now = c.clock.now()
    metrics = metrics_table()
    units = contracts.units()
    res = BatchResult()
    rows: list[dict[str, Any]] = []
    first_in_batch: dict[tuple[Any, ...], tuple[int, Normalized]] = {}
    pending: list[tuple[int, Normalized]] = []
    for i, item in enumerate(items):
        try:
            n = normalize(item, metrics=metrics, units=units, source_timezone=device.source_timezone, now=now,
                          future_tolerance=timedelta(seconds=s.observation_future_tolerance_s),
                          clock_offset_ms=clock_offset_ms, clock_offset_warn_ms=s.clock_offset_warn_ms,
                          time_format=time_format)
        except Rejected as exc:
            res.add(i, "REJECTED", exc.reason)
            continue
        if n.key() in first_in_batch:  # 같은 배치 안의 중복
            _, first = first_in_batch[n.key()]
            ok = same_value(first.value, n.value) and first.quality_flag == n.quality_flag
            res.add(i, "DUPLICATE" if ok else "REJECTED", None if ok else "VALUE_CONFLICT")
            continue
        first_in_batch[n.key()] = (i, n)
        pending.append((i, n))
        rows.append({"organization_id": organization_id, "source_system_id": device.device_id, "metric": n.metric,
                     "observed_at": n.observed_at, "depth_m": n.depth_m, "channel": n.channel, "value": n.value,
                     "unit": n.unit, "original_value": n.original_value, "original_unit": n.original_unit,
                     "quality_flag": n.quality_flag, "source_timestamp": n.source_timestamp,
                     "clock_offset_ms": clock_offset_ms, "source_asset_id": source_asset_id,
                     "session_id": session_id, "received_at": now})
    inserted: set[tuple[Any, ...]] = set()
    for start in range(0, len(rows), 1000):
        chunk = rows[start:start + 1000]
        stmt = insert(OBS).values(chunk).on_conflict_do_nothing(index_elements=list(KEY_COLS)) \
            .returning(OBS.c.metric, OBS.c.observed_at, OBS.c.depth_m, OBS.c.channel)
        inserted |= {(r[0], r[1], r[2], r[3]) for r in db.execute(stmt)}
    for i, n in pending:
        if n.key() in inserted:
            res.add(i, "ACCEPTED")
            continue
        existing = db.execute(select(OBS.c.value, OBS.c.quality_flag).where(
            OBS.c.organization_id == organization_id, OBS.c.source_system_id == device.device_id,
            OBS.c.metric == n.metric, OBS.c.observed_at == n.observed_at,
            OBS.c.depth_m.is_(None) if n.depth_m is None else OBS.c.depth_m == n.depth_m,
            OBS.c.channel.is_(None) if n.channel is None else OBS.c.channel == n.channel)).first()
        ok = existing is not None and same_value(existing[0], n.value)
        res.add(i, "DUPLICATE" if ok else "REJECTED", None if ok else "VALUE_CONFLICT")
    return res


def record_view(r: Any) -> dict[str, Any]:
    from app.timefmt import iso

    return {"source_system_id": r.source_system_id, "metric": r.metric, "observed_at": iso(r.observed_at),
            "value": r.value, "unit": r.unit, "original_value": r.original_value, "original_unit": r.original_unit,
            "quality_flag": r.quality_flag, "source_timestamp": r.source_timestamp,
            "clock_offset_ms": r.clock_offset_ms, "depth_m": r.depth_m, "source_asset_id": r.source_asset_id}


def correct(c: Container, db: Session, p: Principal, body: dict[str, Any]) -> dict[str, Any] | None:
    """ORG_ADMIN 정정: 새 값으로 바꾸고 이전 값·사유를 이력에 남긴다 (요구사항 3.2)."""
    from app.domain.normalize import parse_rfc3339

    when = parse_rfc3339(body["observed_at"])
    cond = and_(OBS.c.organization_id == p.organization_id, OBS.c.source_system_id == body["source_system_id"],
                OBS.c.metric == body["metric"], OBS.c.observed_at == when,
                OBS.c.depth_m.is_(None) if body.get("depth_m") is None else OBS.c.depth_m == body["depth_m"],
                OBS.c.channel.is_(None) if body.get("channel") is None else OBS.c.channel == body["channel"])
    cur = db.execute(select(OBS).where(cond).with_for_update()).first()
    if cur is None:
        return None
    new_flag = body.get("quality_flag") or ("MISSING" if body["value"] is None else "GOOD")
    if (body["value"] is None) != (new_flag == "MISSING"):
        from app.errors import ProblemError

        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/quality_flag", "code": "missing_flag",
                                                        "message": "결측(null)과 MISSING은 함께 씁니다"}])
    db.add(ObservationCorrection(organization_id=p.organization_id, source_system_id=cur.source_system_id,
                                 metric=cur.metric, observed_at=cur.observed_at, depth_m=cur.depth_m,
                                 channel=cur.channel, old_value=cur.value, old_quality_flag=cur.quality_flag,
                                 new_value=body["value"], new_quality_flag=new_flag, reason=body["reason"],
                                 corrected_by=p.principal_id, corrected_at=c.clock.now()))
    db.execute(OBS.update().where(cond).values(value=body["value"], quality_flag=new_flag))
    return record_view(db.execute(select(OBS).where(cond)).first())


# ---------------------------------------------------------------- 매핑 프로필

def current_mapping(db: Session, device_id: str) -> ObservationMapping | None:
    return db.execute(select(ObservationMapping).where(ObservationMapping.device_id == device_id)
                      .order_by(ObservationMapping.version.desc()).limit(1)).scalar_one_or_none()


def next_mapping_version(db: Session, device_id: str) -> int:
    return int(db.execute(select(func.coalesce(func.max(ObservationMapping.version), 0))
                          .where(ObservationMapping.device_id == device_id)).scalar_one()) + 1


# ---------------------------------------------------------------- 센서 파일 파싱

@dataclass
class ParsedRows:
    items: list[dict[str, Any]]
    row_of_item: list[int]
    row_errors: list[dict[str, Any]]


def _num(raw: str, missing: set[str]) -> tuple[float | None, bool]:
    t = raw.strip()
    if t in missing:
        return None, True
    return float(t), False


def parse_rows(content: bytes, mapping: dict[str, Any]) -> ParsedRows:
    """매핑 프로필로 CSV·JSON Lines를 관측값 항목으로 바꾼다. 행 번호는 1부터 (헤더 포함)."""
    missing = set(mapping.get("missing_values") or ["", "NA", "N/A", "null"])
    ts = mapping.get("timestamp") or {"field": "time"}
    fmt = mapping.get("format", "csv")
    items: list[dict[str, Any]] = []
    rows_of: list[int] = []
    errors: list[dict[str, Any]] = []
    text_content = content.decode("utf-8-sig")
    if fmt == "csv":
        header_row = int(mapping.get("header_row", 1))
        lines = text_content.splitlines()
        reader = csv.DictReader(io.StringIO("\n".join(lines[header_row - 1:])), delimiter=mapping.get("delimiter", ","))
        missing_cols = [f for f in [ts["field"], *(c["field"] for c in mapping["columns"])]
                        if f not in (reader.fieldnames or [])]
        if missing_cols:
            return ParsedRows([], [], [{"row": header_row, "reason": "COLUMN_NOT_FOUND",
                                        "detail": ", ".join(missing_cols)}])
        records = [(header_row + n, r) for n, r in enumerate(reader, start=1)]
    elif fmt == "json_lines":
        records = []
        for n, line in enumerate(text_content.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                records.append((n, json.loads(line)))
            except json.JSONDecodeError:
                errors.append({"row": n, "reason": "INVALID_JSON"})
    else:
        return ParsedRows([], [], [{"row": 0, "reason": "UNSUPPORTED_FORMAT", "detail": fmt}])
    for row_no, rec in records:
        raw_ts = rec.get(ts["field"])
        if raw_ts in (None, ""):
            errors.append({"row": row_no, "reason": "TIMESTAMP_MISSING"})
            continue
        for col in mapping["columns"]:
            raw = rec.get(col["field"])
            try:
                value, is_missing = _num(str(raw) if raw is not None else "", missing)
            except ValueError:
                errors.append({"row": row_no, "reason": "NOT_A_NUMBER", "field": col["field"]})
                continue
            item: dict[str, Any] = {"metric": col["metric"], "value": value,
                                    "quality_flag": "MISSING" if is_missing else "GOOD"}
            if col.get("quality_field") and rec.get(col["quality_field"]) not in (None, "") and not is_missing:
                item["quality_flag"] = str(rec[col["quality_field"]]).upper()
            if col.get("unit") and not is_missing:
                item["unit"] = col["unit"]
            for k in ("depth_m", "channel"):
                if col.get(k) is not None:
                    item[k] = col[k]
            raw_ts_s = str(raw_ts)
            if ts.get("format"):
                item["source_timestamp"] = raw_ts_s  # 형식 지정 시 장비 시간대로 해석
            elif any(z in raw_ts_s[10:] for z in ("Z", "+", "-")):
                item["observed_at"] = raw_ts_s
            else:
                item["source_timestamp"] = raw_ts_s
            items.append(item)
            rows_of.append(row_no)
    return ParsedRows(items, rows_of, errors)
