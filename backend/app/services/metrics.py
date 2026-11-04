"""지표 계산 (10번 요구사항 3). 값과 함께 계산식·입력 건수·누락 건수·원천 목록을 남긴다.

- 근거가 없으면 값을 추정하지 않고 NO_DATA (요구사항 3.3).
- 사용한 이벤트가 모두 모델 결과(source=MODEL, human_verified=false)면 NEEDS_VERIFICATION — 확정값으로 내보내지 않는다
  (요구사항 3.4). 이벤트 외 정답 데이터(annotation_kind=PREDICTION 구분)는 14번에서 더한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.db.models import Asset, Event, EventAsset, MetricDefinition, SessionDevice
from app.services.observations import OBS
from app.timefmt import iso

KINDS = ("EVENT_COUNT", "EVENT_RATIO", "OBSERVATION_STAT", "VIDEO_HOURS")
STATS = ("mean", "min", "max", "count")


@dataclass(frozen=True)
class Scope:
    """계산 범위: 회차 하나 또는 사업·기간."""

    organization_id: str
    project_id: str
    session_id: str | None
    start: datetime
    end: datetime

    def describe(self) -> str:
        return f"회차 {self.session_id}" if self.session_id else f"{iso(self.start)} ~ {iso(self.end)}"


def validate(kind: str, params: dict[str, Any]) -> list[str]:
    """지표 정의 등록 검사. 계산 방법(kind)별 필수 값."""
    def codes(key: str) -> bool:
        v = params.get(key)
        return isinstance(v, list) and bool(v) and all(isinstance(x, str) and x for x in v)

    if kind == "EVENT_COUNT":
        return [] if codes("event_codes") else ["params.event_codes: 이벤트 코드 목록이 필요합니다"]
    if kind == "EVENT_RATIO":
        return [f"params.{k}: 이벤트 코드 목록이 필요합니다" for k in ("numerator_codes", "denominator_codes")
                if not codes(k)]
    if kind == "OBSERVATION_STAT":
        errs = [] if isinstance(params.get("metric"), str) else ["params.metric: 관측 항목 코드가 필요합니다"]
        return errs + ([] if params.get("stat") in STATS else [f"params.stat: {', '.join(STATS)} 중 하나"])
    if kind == "VIDEO_HOURS":
        return []
    return [f"kind: {', '.join(KINDS)} 중 하나"]


def formula(kind: str, params: dict[str, Any]) -> str:
    if kind == "EVENT_COUNT":
        return f"count(이벤트: event_code ∈ {{{', '.join(params['event_codes'])}}}, 하위코드 포함)"
    if kind == "EVENT_RATIO":
        return (f"count(이벤트 ∈ {{{', '.join(params['numerator_codes'])}}}) / "
                f"count(이벤트 ∈ {{{', '.join(params['denominator_codes'])}}})")
    if kind == "OBSERVATION_STAT":
        return f"{params['stat']}(관측값 {params['metric']}, quality_flag ∉ {{BAD, MISSING}})"
    return "sum(원본 영상 길이) / 3600 (격리·삭제 제외)"


def latest(db: Session, code: str) -> MetricDefinition | None:
    return db.execute(select(MetricDefinition).where(MetricDefinition.code == code)
                      .order_by(MetricDefinition.version.desc()).limit(1)).scalar_one_or_none()


def _events(db: Session, s: Scope, codes: list[str]) -> list[Event]:
    # 상위분류 코드(예: VESSEL)는 기관 하위코드(VESSEL.small_boat)까지 센다 (03번 Code_Table 규칙)
    match = or_(Event.event_code.in_(codes), *[Event.event_code.like(f"{x}.%") for x in codes if "." not in x])
    q = select(Event).where(Event.organization_id == s.organization_id, match)
    if s.session_id:
        q = q.where(Event.session_id == s.session_id)
    else:
        q = q.where(Event.project_id == s.project_id, Event.started_at >= s.start, Event.started_at < s.end)
    return list(db.execute(q.order_by(Event.started_at, Event.event_id)).scalars())


def _session_devices(db: Session, s: Scope) -> list[str]:
    if not s.session_id:
        return []
    return list(db.execute(select(SessionDevice.device_id).where(SessionDevice.session_id == s.session_id)).scalars())


def _asset_scope(s: Scope) -> Any:
    base = and_(Asset.organization_id == s.organization_id, Asset.deleted_at.is_(None))
    if s.session_id:
        return and_(base, Asset.session_id == s.session_id)
    return and_(base, Asset.project_id == s.project_id, Asset.capture_start_at >= s.start,
                Asset.capture_start_at < s.end)


def has_any_data(db: Session, s: Scope) -> bool:
    """범위 안에 자산·이벤트·관측값이 하나라도 있는가 (0건과 '기록 없음'을 가른다)."""
    if db.execute(select(func.count()).select_from(Asset).where(_asset_scope(s))).scalar_one():
        return True
    q = select(func.count()).select_from(Event).where(Event.organization_id == s.organization_id)
    q = q.where(Event.session_id == s.session_id) if s.session_id else \
        q.where(Event.project_id == s.project_id, Event.started_at >= s.start, Event.started_at < s.end)
    if db.execute(q).scalar_one():
        return True
    return bool(db.execute(select(func.count()).select_from(OBS).where(*_obs_where(db, s))).scalar_one())


def _obs_where(db: Session, s: Scope) -> list[Any]:
    conds: list[Any] = [OBS.c.organization_id == s.organization_id]
    if s.session_id:
        devices = _session_devices(db, s)
        conds.append(or_(OBS.c.session_id == s.session_id,
                         and_(OBS.c.source_system_id.in_(devices), OBS.c.observed_at >= s.start,
                              OBS.c.observed_at < s.end)) if devices else OBS.c.session_id == s.session_id)
    else:
        conds += [OBS.c.observed_at >= s.start, OBS.c.observed_at < s.end]
    return conds


def _model_only(events: list[Event]) -> bool:
    return bool(events) and all(e.source == "MODEL" and not e.human_verified for e in events)


def _event_assets(db: Session, events: list[Event]) -> list[str]:
    if not events:
        return []
    return sorted(set(db.execute(select(EventAsset.asset_id).where(
        EventAsset.event_id.in_([e.event_id for e in events]))).scalars()))


def compute(db: Session, d: MetricDefinition, s: Scope, now: datetime) -> dict[str, Any]:
    p = d.params
    out: dict[str, Any] = {"metric_code": d.code, "metric_version": d.version, "name": d.name, "unit": d.unit,
                           "formula": formula(d.kind, p), "computed_at": iso(now), "value": None,
                           "input_count": 0, "missing_count": 0,
                           "sources": {"event_ids": [], "asset_ids": [], "observation_ranges": []}}
    if d.kind in ("EVENT_COUNT", "EVENT_RATIO"):
        if d.kind == "EVENT_COUNT":
            num = _events(db, s, p["event_codes"])
            den: list[Event] = []
        else:
            num, den = _events(db, s, p["numerator_codes"]), _events(db, s, p["denominator_codes"])
        used = {e.event_id: e for e in [*num, *den]}
        out["sources"]["event_ids"] = sorted(used)
        out["sources"]["asset_ids"] = _event_assets(db, list(used.values()))
        out["input_count"] = len(used)
        if d.kind == "EVENT_COUNT":
            if not num and not has_any_data(db, s):
                return {**out, "status": "NO_DATA"}  # 0건과 '기록이 없음'을 구분한다
            out["value"] = len(num)
        else:
            if not den:
                return {**out, "status": "NO_DATA"}
            out["value"] = round(len(num) / len(den), 6)
        return {**out, "status": "NEEDS_VERIFICATION" if _model_only(num or den) else "OK"}
    if d.kind == "OBSERVATION_STAT":
        where = [*_obs_where(db, s), OBS.c.metric == p["metric"]]
        usable = and_(OBS.c.quality_flag.not_in(["BAD", "MISSING"]), OBS.c.value.is_not(None))
        stat = {"mean": func.avg(OBS.c.value), "min": func.min(OBS.c.value), "max": func.max(OBS.c.value),
                "count": func.count()}[p["stat"]]
        n_all = db.execute(select(func.count()).select_from(OBS).where(*where)).scalar_one()
        row = db.execute(select(func.count(), stat, func.min(OBS.c.observed_at), func.max(OBS.c.observed_at))
                         .select_from(OBS).where(*where, usable)).one()
        n_ok = int(row[0])
        out["input_count"], out["missing_count"] = n_ok, int(n_all) - n_ok
        ranges = db.execute(select(OBS.c.source_system_id, func.min(OBS.c.observed_at), func.max(OBS.c.observed_at),
                                   func.count()).select_from(OBS).where(*where, usable)
                            .group_by(OBS.c.source_system_id)).all()
        out["sources"]["observation_ranges"] = [
            {"source_system_id": dev, "metric": p["metric"], "from": iso(a), "to": iso(b), "count": int(n)}
            for dev, a, b, n in ranges]
        if n_ok == 0:
            return {**out, "status": "NO_DATA"}
        out["value"] = n_ok if p["stat"] == "count" else round(float(row[1]), 6)
        return {**out, "status": "OK"}
    # VIDEO_HOURS
    rows = list(db.execute(select(Asset).where(_asset_scope(s), Asset.data_type == "VIDEO", Asset.variant == "ORIGINAL",
                                               Asset.ingest_status != "QUARANTINED")).scalars())
    durations = {a.asset_id: ((a.ext or {}).get("video_ext") or {}).get("duration_s") for a in rows}
    known = {k: float(v) for k, v in durations.items() if isinstance(v, int | float)}
    out["input_count"], out["missing_count"] = len(known), len(rows) - len(known)
    out["sources"]["asset_ids"] = sorted(known)
    if not known:
        return {**out, "status": "NO_DATA"}
    out["value"] = round(sum(known.values()) / 3600, 4)
    return {**out, "status": "OK"}
