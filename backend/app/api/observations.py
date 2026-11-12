"""관측값·매핑 프로필·정정·실증 이벤트 API (07번)."""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, func, literal, or_, select, text, tuple_
from sqlalchemy.orm import Session

from app.api.deps import client_ip, container, get_db, load_scoped, page_params, require
from app.api.pagination import decode_cursor, encode_cursor, paginate
from app.container import Container
from app.db.models import Asset, Device, Event, EventAsset, ObservationMapping, OrgEventCode, TrialSession
from app.domain.ids import has_prefix, new_id
from app.domain.normalize import TimeRuleError, parse_rfc3339
from app.domain.principal import Principal
from app.errors import ProblemError
from app.services import contracts
from app.services.idempotency import run_idempotent
from app.services.observations import (
    OBS,
    correct,
    current_mapping,
    ingest_items,
    next_mapping_version,
    record_view,
)
from app.services.tenant import scoped_select
from app.timefmt import iso

router = APIRouter()
_UTC_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?Z")


def _validate_utc_timestamp(value: str | None) -> str:
    if value is None or not _UTC_TIMESTAMP.fullmatch(value):
        raise ValueError("Z로 끝나는 RFC 3339 UTC 시각을 입력하세요")
    try:
        parse_rfc3339(value)
    except TimeRuleError:
        raise ValueError("유효한 날짜와 시각을 입력하세요") from None
    return value


def _problems_to_400(problems: list[Any], rejected: bool) -> None:
    if rejected:
        raise ProblemError("UNSUPPORTED_SCHEMA_VERSION", detail=problems[0].message)
    errors = [x for x in problems if x.severity == "ERROR"]
    if errors:
        shown = [{"pointer": x.pointer, "code": x.code, "message": x.message} for x in errors[:contracts.MAX_PROBLEMS]]
        if len(errors) > contracts.MAX_PROBLEMS:  # 응답이 요청보다 커지지 않게 앞의 문제만 보여준다
            notice = contracts.truncation_notice()
            shown.append({"pointer": notice.pointer, "code": notice.code, "message": notice.message})
        raise ProblemError("VALIDATION_ERROR", errors=shown)


def _source_device(db: Session, c: Container, request: Request, p: Principal, device_id: str) -> Device:
    device = load_scoped(db, c, request, Device, device_id, p, audit_device_scope=True)
    if device.status == "REVOKED":
        raise ProblemError("RULE_VIOLATION", detail="폐기된 장비입니다",
                           unmet_conditions=[{"condition": "device.status=ACTIVE", "actual": device.status}])
    return device


# ---------------------------------------------------------------- 관측값

@router.post("/v1/observations", tags=["observations"])
def post_observations(request: Request, body: Any = Body(...), db: Session = Depends(get_db),
                      c: Container = Depends(container), p: Principal = Depends(require("observation:write")),
                      idempotency_key: str | None = Header(default=None)) -> JSONResponse:
    # 항목 수 한도는 스키마 검증보다 먼저 (요구사항 1.4)
    if isinstance(body, dict) and isinstance(body.get("observations"), list) and \
            len(body["observations"]) > c.settings.observation_batch_max:
        raise ProblemError("PAYLOAD_TOO_LARGE", detail=f"배치 항목은 {c.settings.observation_batch_max}개까지입니다")
    # 항목별 판정(REJECTED 사유)에는 모든 항목의 문제가 필요하므로 목록을 100개에서 자르지 않는다.
    # (잘리면 뒤쪽 잘못된 항목이 검사 없이 통과한다.) 항목 수 한도가 있어 5만 개를 넘는 일은 방어용이다
    problems, rejected = contracts.validate_document("observation-batch", body, limit=contracts.ITEM_PROBLEM_LIMIT)
    if contracts.is_truncated(problems):  # 항목별로 가릴 수 없으니 배치 전체를 거부한다
        _problems_to_400(problems, False)
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "", "code": "too_many_problems",
                                                        "message": "문제가 너무 많아 배치를 검사하지 못했습니다"}])
    # 항목의 시각·결측 표기·항목 코드 문제는 400이 아니라 항목별 REJECTED (요구사항 1.3, 2.2, 2.4).
    # 그 밖의 형식 오류는 배치 전체 400, 아무것도 저장하지 않는다 (요구사항 1.2)
    item_reasons: dict[int, str] = {}
    fatal = []
    for x in problems:
        reason = _item_reason(x)
        if reason is None:
            fatal.append(x)
        else:
            idx = int(x.pointer.split("/")[2])
            item_reasons.setdefault(idx, reason)
    _problems_to_400(fatal, rejected)
    device = _source_device(db, c, request, p, body["source_system_id"])
    session_id = body.get("session_id")
    if session_id:
        load_scoped(db, c, request, TrialSession, session_id, p)

    def run() -> tuple[int, dict[str, Any]]:
        bad_items = set(item_reasons)
        items = body["observations"]
        res = ingest_items(c, db, organization_id=p.organization_id, device=device,
                           items=[it for i, it in enumerate(items) if i not in bad_items],
                           session_id=session_id, clock_offset_ms=body.get("clock_offset_ms"))
        # 스키마 단계에서 걸린 항목은 사유를 정해 REJECTED로 (인덱스를 원래 위치로 되돌린다)
        kept = [i for i in range(len(items)) if i not in bad_items]
        out = res.as_dict()
        out["results"] = [{**r, "index": kept[r["index"]]} for r in out["results"]]
        for i in sorted(bad_items):
            out["results"].append({"index": i, "status": "REJECTED", "reason": item_reasons[i]})
            out["rejected"] += 1
        out["results"].sort(key=lambda r: r["index"])
        return 200, out

    status, resp, replayed = run_idempotent(db, c.clock, c.settings.idempotency_retention_s, p, "POST /v1/observations",
                                            idempotency_key, body, run)
    return JSONResponse(resp, status_code=status, headers={"Idempotent-Replayed": "true"} if replayed else None)


_ITEM_FIELD_REASON = {"observed_at": "TIMEZONE_UNKNOWN", "source_timestamp": "TIMEZONE_UNKNOWN",
                      "value": "MISSING_FLAG_MISMATCH", "quality_flag": "MISSING_FLAG_MISMATCH",
                      "unit": "MISSING_FLAG_MISMATCH", "metric": "UNKNOWN_METRIC"}


def _item_reason(x: Any) -> str | None:
    """항목 단위로 돌려줄 수 있는 스키마·도메인 문제면 거부 사유, 아니면 None (배치 전체 400)."""
    parts = x.pointer.split("/")
    if len(parts) < 3 or parts[1] != "observations" or not parts[2].isdigit():
        return None
    if x.code == "timezone_required":
        return "TIMEZONE_UNKNOWN"
    if x.code == "missing_flag":
        return "MISSING_FLAG_MISMATCH"
    if x.severity != "ERROR":
        return None
    if len(parts) >= 4:
        return _ITEM_FIELD_REASON.get(parts[3])
    if x.code == "schema.required" and ("observed_at" in x.message or "source_timestamp" in x.message):
        return "TIMEZONE_UNKNOWN"
    if x.code == "schema.required" and ("value" in x.message or "unit" in x.message):
        return "MISSING_FLAG_MISMATCH"
    return None


def _obs_scope(p: Principal) -> Any:
    if p.has_role("PLATFORM_OPERATOR") and not p.is_machine:
        return literal(True)
    return OBS.c.organization_id == p.organization_id


_ORDER = (OBS.c.observed_at, OBS.c.source_system_id, OBS.c.metric, func.coalesce(OBS.c.depth_m, -1e300),
          func.coalesce(OBS.c.channel, ""))


def _cursor_encode(r: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps([iso(r.observed_at), r.source_system_id, r.metric,
                                                -1e300 if r.depth_m is None else r.depth_m, r.channel or ""])
                                    .encode()).decode().rstrip("=")


def _cursor_decode(cur: str) -> list[Any]:
    try:
        v = json.loads(base64.urlsafe_b64decode(cur + "=" * (-len(cur) % 4)))
        v[0] = parse_rfc3339(v[0])
        return list(v)
    except (ValueError, KeyError, IndexError, TypeError):
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/query/cursor", "code": "invalid",
                                                        "message": "커서 형식이 올바르지 않습니다"}]) from None


@router.get("/v1/observations", tags=["observations"])
def list_observations(source_system_id: str | None = None, metric: str | None = None, session_id: str | None = None,
                      from_: datetime = Query(alias="from"), to: datetime = Query(),
                      quality_flag: str | None = None, channel: str | None = None,
                      interval: str | None = Query(default=None, pattern=r"^PT[1-9][0-9]{0,5}[MH]$"),
                      db: Session = Depends(get_db), p: Principal = Depends(require("observation:read")),
                      page: dict = Depends(page_params)) -> dict:
    if from_.tzinfo is None or to.tzinfo is None or to <= from_:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/query/to", "code": "range",
                                                        "message": "from < to, 시각은 UTC(Z)"}])
    conds = [_obs_scope(p), OBS.c.observed_at >= from_, OBS.c.observed_at < to]
    for col, val in ((OBS.c.source_system_id, source_system_id), (OBS.c.metric, metric),
                     (OBS.c.session_id, session_id),  # 09번 요구사항 5.2: 회차로 표시된 관측값만
                     (OBS.c.quality_flag, quality_flag), (OBS.c.channel, channel)):
        if val is not None:
            conds.append(col == val)
    if interval:  # 구간 집계: 결측은 평균에 넣지 않고 따로 센다 (요구사항 8.2)
        if not metric:
            raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/query/metric", "code": "required",
                                                            "message": "집계에는 metric이 필요합니다"}])
        n, unit = int(interval[2:-1]), interval[-1]
        if n > (525_600 if unit == "M" else 8_760):  # 1년 넘는 간격은 집계 의미가 없고 DB 간격 범위를 넘을 수 있다
            raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/query/interval", "code": "range",
                                                            "message": "집계 간격은 1분~1년입니다"}])
        step = f"{n} {'minutes' if unit == 'M' else 'hours'}"
        bucket = func.date_bin(text(f"interval '{step}'"), OBS.c.observed_at, from_).label("bucket")
        rows = db.execute(select(bucket, func.min(OBS.c.value), func.max(OBS.c.value), func.avg(OBS.c.value),
                                 func.count(), func.count().filter(OBS.c.quality_flag == "MISSING"))
                          .where(and_(*conds)).group_by(text("1")).order_by(text("1"))).all()  # 같은 식으로 묶기
        return {"interval": interval, "items": [
            {"bucket_start": iso(r[0]), "min": r[1], "max": r[2], "avg": float(r[3]) if r[3] is not None else None,
             "count": int(r[4]), "missing_count": int(r[5])} for r in rows]}
    stmt = select(OBS).where(and_(*conds))
    if page["cursor"]:
        stmt = stmt.where(tuple_(*_ORDER) > tuple_(*_cursor_decode(page["cursor"])))
    rows = db.execute(stmt.order_by(*_ORDER).limit(page["limit"] + 1)).all()
    nxt = _cursor_encode(rows[page["limit"] - 1]) if len(rows) > page["limit"] else None
    return {"items": [record_view(r) for r in rows[:page["limit"]]], "next_cursor": nxt}


class Correction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_system_id: str = Field(pattern=r"^dev_[0-9A-HJKMNP-TV-Z]{26}$")
    metric: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    observed_at: str
    depth_m: float | None = None
    channel: str | None = Field(default=None, max_length=32)
    value: float | None
    quality_flag: str | None = Field(default=None, pattern="^(GOOD|SUSPECT|BAD|MISSING)$")
    reason: str = Field(min_length=2, max_length=500)

    _check_observed_at = field_validator("observed_at")(_validate_utc_timestamp)


@router.post("/v1/observations/corrections", tags=["observations"])
def correct_observation(body: Correction, request: Request, db: Session = Depends(get_db),
                        c: Container = Depends(container),
                        p: Principal = Depends(require("observation:correct"))) -> dict:
    load_scoped(db, c, request, Device, body.source_system_id, p)
    out = correct(c, db, p, body.model_dump())
    if out is None:
        raise ProblemError("NOT_FOUND", detail="해당 관측값이 없습니다")
    c.audit.record("OBSERVATION_CORRECTED", principal=p, organization_id=p.organization_id,
                   target_type="Observation", target_id=body.source_system_id, reason=body.reason,
                   source_ip=client_ip(request), details={"metric": body.metric, "observed_at": body.observed_at,
                                                          "depth_m": body.depth_m, "channel": body.channel,
                                                          "new_value": body.value})
    return out


# ---------------------------------------------------------------- 매핑 프로필

def _mapping_view(m: ObservationMapping) -> dict:
    return {"device_id": m.device_id, "version": m.version, "mapping": m.mapping, "created_at": iso(m.created_at)}


@router.get("/v1/devices/{device_id}/observation-mapping", tags=["observations"])
def get_mapping(device_id: str, request: Request, db: Session = Depends(get_db), c: Container = Depends(container),
                p: Principal = Depends(require("mapping:read"))) -> dict:
    load_scoped(db, c, request, Device, device_id, p)
    if p.kind == "DEVICE" and device_id not in p.mapped_source_ids:
        raise ProblemError("NOT_FOUND")
    m = current_mapping(db, device_id)
    if m is None:
        raise ProblemError("NOT_FOUND", detail="매핑 프로필이 없습니다")
    return _mapping_view(m)


class MappingColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str = Field(max_length=64)
    metric: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    unit: str | None = Field(default=None, max_length=32)
    depth_m: float | None = None
    channel: str | None = Field(default=None, max_length=32)
    quality_field: str | None = Field(default=None, max_length=64)


class MappingTimestamp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str = Field(max_length=64)
    format: str | None = Field(default=None, max_length=64)


class MappingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: str = Field(pattern="^(csv|json_lines|mqtt_json)$")
    delimiter: str | None = Field(default=None, min_length=1, max_length=1)
    header_row: int | None = Field(default=None, ge=1)
    timestamp: MappingTimestamp | None = None
    missing_values: list[str] | None = None
    topic_filter: str | None = Field(default=None, max_length=256)
    columns: list[MappingColumn] = Field(min_length=1)


@router.put("/v1/devices/{device_id}/observation-mapping", tags=["observations"])
def put_mapping(device_id: str, body: MappingBody, request: Request, db: Session = Depends(get_db),
                c: Container = Depends(container), p: Principal = Depends(require("mapping:write"))) -> dict:
    device = load_scoped(db, c, request, Device, device_id, p)
    metrics = {m["code"] for m in contracts.code_table("metrics")["items"]}
    unknown = [col.metric for col in body.columns if col.metric not in metrics]
    if unknown:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/columns", "code": "unknown_metric",
                                                        "message": f"코드표에 없는 항목: {', '.join(unknown)}"}])
    before = current_mapping(db, device_id)
    m = ObservationMapping(device_id=device_id, version=next_mapping_version(db, device_id),
                           organization_id=device.organization_id, mapping=body.model_dump(exclude_none=True),
                           created_by=p.principal_id, created_at=c.clock.now())
    db.add(m)
    db.flush()
    c.audit.record("OBSERVATION_MAPPING_CHANGED", principal=p, organization_id=device.organization_id,
                   target_type="Device", target_id=device_id, source_ip=client_ip(request),
                   details={"before": before.mapping if before else None, "after": m.mapping,
                            "version": m.version})  # 수정 전후 (요구사항 4.3)
    return _mapping_view(m)


# ---------------------------------------------------------------- 실증 이벤트

_EVENT_FIELDS = ("schema_version", "session_id", "source_system_id", "event_code", "timestamp_quality", "location",
                 "severity", "source", "model_ref", "confidence", "source_event_id", "note")


def event_view(db: Session, e: Event) -> dict[str, Any]:
    out: dict[str, Any] = {k: getattr(e, k) for k in _EVENT_FIELDS if getattr(e, k) is not None}
    out.update({"event_id": e.event_id, "organization_id": e.organization_id, "started_at": iso(e.started_at),
                "created_at": iso(e.created_at), "human_verified": e.human_verified})
    if e.ended_at:
        out["ended_at"] = iso(e.ended_at)
    if e.verified_by:
        out["verified_by"], out["verified_at"] = e.verified_by, iso(e.verified_at)
    linked = list(db.execute(select(EventAsset.asset_id).where(EventAsset.event_id == e.event_id)
                             .order_by(EventAsset.asset_id)).scalars())
    if linked:
        out["linked_asset_ids"] = linked
    return out


def _check_code(db: Session, p: Principal, code: str) -> None:
    top = code.split(".")[0]
    cats = {x["code"] for x in contracts.code_table("event-categories")["items"]}
    if top not in cats:
        raise ProblemError("RULE_VIOLATION", detail="공통 상위분류에 없는 이벤트 코드입니다",
                           unmet_conditions=[{"condition": "event_code 상위분류 ∈ event-categories", "actual": top}])
    if "." in code and db.get(OrgEventCode, (p.organization_id, code)) is None:  # 요구사항 7.5
        raise ProblemError("RULE_VIOLATION", detail="기관 하위코드가 등록되어 있지 않습니다",
                           unmet_conditions=[{"condition": "기관 하위코드 등록", "actual": code}])


def _link_assets(db: Session, c: Container, request: Request, p: Principal, event: Event, ids: list[str]) -> None:
    for aid in sorted(set(ids)):
        load_scoped(db, c, request, Asset, aid, p)  # 같은 기관 자산만 (요구사항 7.2)
        if db.get(EventAsset, (event.event_id, aid)) is None:
            db.add(EventAsset(event_id=event.event_id, asset_id=aid))


@router.post("/v1/events", tags=["events"])
def create_event(request: Request, body: Any = Body(...), db: Session = Depends(get_db),
                 c: Container = Depends(container), p: Principal = Depends(require("event:write")),
                 idempotency_key: str | None = Header(default=None)) -> JSONResponse:
    problems, rejected = contracts.validate_document("event", body)
    _problems_to_400(problems, rejected)
    if body.get("human_verified") is True and not p.user_id:  # 사람만 확인할 수 있다 (07번 요구사항 7.3, PATCH와 같게)
        raise ProblemError("FORBIDDEN", detail="사람 사용자만 확인 표시를 할 수 있습니다")
    session = load_scoped(db, c, request, TrialSession, body["session_id"], p)
    if body.get("source_system_id"):
        _source_device(db, c, request, p, body["source_system_id"])
    elif p.kind == "DEVICE":
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/source_system_id", "code": "required",
                                                        "message": "수집기는 원천 장비를 밝혀야 합니다"}])
    _check_code(db, p, body["event_code"])
    started = parse_rfc3339(body["started_at"])
    ended = parse_rfc3339(body["ended_at"]) if body.get("ended_at") else None
    if ended is not None and ended < started:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/ended_at", "code": "before_start",
                                                        "message": "종료가 시작보다 빠릅니다"}])

    def create() -> tuple[int, dict[str, Any]]:
        if body.get("source_event_id"):  # 원천 이벤트 ID로 중복 방지 (요구사항 7.4)
            duplicate = db.execute(select(Event).where(
                Event.organization_id == p.organization_id,
                Event.source_system_id == body.get("source_system_id"),
                Event.source_event_id == body["source_event_id"],
            )).scalar_one_or_none()
            if duplicate is not None:
                return 200, event_view(db, duplicate)
        e = Event(event_id=new_id("event"), organization_id=p.organization_id, session_id=session.session_id,
                  project_id=session.project_id, source_system_id=body.get("source_system_id"),
                  event_code=body["event_code"], started_at=started, ended_at=ended,
                  timestamp_quality=body["timestamp_quality"], location=body.get("location"),
                  severity=body.get("severity"), source=body["source"], model_ref=body.get("model_ref"),
                  confidence=body.get("confidence"),
                  # 모델 결과는 사람이 확인하기 전까지 false (domain.md 6장)
                  human_verified=False if body["source"] == "MODEL" else bool(body.get("human_verified", False)),
                  source_event_id=body.get("source_event_id"), note=body.get("note"),
                  schema_version=body["schema_version"], created_by=p.principal_id, created_at=c.clock.now())
        if e.human_verified and p.user_id:
            e.verified_by, e.verified_at = p.user_id, e.created_at
        db.add(e)
        db.flush()
        _link_assets(db, c, request, p, e, body.get("linked_asset_ids") or [])
        db.flush()
        return 201, event_view(db, e)

    status, resp, replayed = run_idempotent(db, c.clock, c.settings.idempotency_retention_s, p, "POST /v1/events",
                                            idempotency_key, body, create)
    return JSONResponse(resp, status_code=status, headers={"Idempotent-Replayed": "true"} if replayed else None)


@router.get("/v1/events", tags=["events"])
def list_events(session_id: str | None = None, event_code: str | None = None, source_system_id: str | None = None,
                human_verified: bool | None = None, from_: datetime | None = Query(default=None, alias="from"),
                to: datetime | None = None, db: Session = Depends(get_db),
                p: Principal = Depends(require("event:read")), page: dict = Depends(page_params)) -> dict:
    stmt = scoped_select(Event, p)
    for col, val in ((Event.session_id, session_id), (Event.source_system_id, source_system_id),
                     (Event.human_verified, human_verified)):
        if val is not None:
            stmt = stmt.where(col == val)
    if event_code:  # 상위분류로 찾으면 하위코드까지
        stmt = stmt.where(or_(Event.event_code == event_code, Event.event_code.like(event_code + ".%")))
    if from_ is not None:
        stmt = stmt.where(or_(Event.ended_at >= from_, and_(Event.ended_at.is_(None), Event.started_at >= from_)))
    if to is not None:
        stmt = stmt.where(Event.started_at < to)
    rows, nxt = paginate(db, stmt, Event.event_id, page["limit"], page["cursor"])
    return {"items": [event_view(db, e) for e in rows], "next_cursor": nxt}


@router.get("/v1/events/{event_id}", tags=["events"])
def get_event(event_id: str, request: Request, db: Session = Depends(get_db), c: Container = Depends(container),
              p: Principal = Depends(require("event:read"))) -> dict:
    return event_view(db, load_scoped(db, c, request, Event, event_id, p))


def _overlap_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        value = json.loads(decode_cursor(cursor))
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError
        captured_at, asset_id = value
        if not isinstance(captured_at, str) or not isinstance(asset_id, str) or len(asset_id) != 30:
            raise ValueError
        if not has_prefix(asset_id, "asset"):
            raise ValueError
        return parse_rfc3339(captured_at), asset_id
    except (ValueError, OverflowError):
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/query/cursor", "code": "invalid",
                                                        "message": "커서 형식이 올바르지 않습니다"}]) from None


@router.get("/v1/events/{event_id}/overlapping-assets", tags=["events"])
def overlapping_assets(event_id: str, request: Request, margin_s: int = Query(0, ge=0, le=3600),
                       source_system_id: str | None = Query(None, pattern=r"^dev_[0-9A-HJKMNP-TV-Z]{26}$"),
                       limit: int = Query(500, ge=1, le=500),
                       cursor: str | None = Query(None, min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_-]+$"),
                       db: Session = Depends(get_db), c: Container = Depends(container),
                       p: Principal = Depends(require("event:read")),
                       _asset_reader: Principal = Depends(require("asset:read"))) -> dict:
    """이벤트 구간과 촬영 구간이 겹치는 같은 회차 자산 (요구사항 7.6). 촬영시각 미상은 개수만."""
    from datetime import timedelta

    from app.api.assets import asset_views

    e = load_scoped(db, c, request, Event, event_id, p)
    start = e.started_at - timedelta(seconds=margin_s)
    end = (e.ended_at or e.started_at) + timedelta(seconds=margin_s)
    base = scoped_select(Asset, p).where(Asset.session_id == e.session_id)
    if source_system_id is not None:
        base = base.where(Asset.source_system_id == source_system_id)
    a_end = func.coalesce(Asset.capture_end_at, Asset.capture_start_at)
    order = (Asset.capture_start_at, Asset.asset_id)
    page = base.where(Asset.capture_start_at.is_not(None), Asset.capture_start_at <= end, a_end >= start)
    if cursor is not None:
        captured_at, asset_id = _overlap_cursor(cursor)
        page = page.where(tuple_(*order) > tuple_(literal(captured_at), literal(asset_id)))
    rows = list(db.scalars(page.order_by(*order).limit(limit + 1)))
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        assert last.capture_start_at is not None  # noqa: S101 - page의 IS NOT NULL
        next_cursor = encode_cursor(json.dumps([last.capture_start_at.isoformat(), last.asset_id]))
    unknown = db.execute(select(func.count()).select_from(
        base.where(Asset.capture_start_at.is_(None)).subquery())).scalar_one()
    linked = set(db.scalars(select(EventAsset.asset_id).where(
        EventAsset.event_id == e.event_id, EventAsset.asset_id.in_([asset.asset_id for asset in rows]),
    )))
    items = []
    for a, view in zip(rows, asset_views(db, rows, p), strict=True):
        a_start = a.capture_start_at
        assert a_start is not None  # noqa: S101 - 위 조건
        overlap = (min(a.capture_end_at or a_start, end) - max(a_start, start)).total_seconds()
        items.append({"asset": view, "overlap_s": max(0.0, overlap), "linked": a.asset_id in linked})
    return {"event_id": e.event_id, "session_id": e.session_id, "window": {"from": iso(start), "to": iso(end)},
            "items": items, "unknown_capture_count": int(unknown), "next_cursor": next_cursor}


class EventPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    human_verified: bool | None = None
    ended_at: str | None = None
    add_linked_asset_ids: list[str] | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=2000)

    _check_ended_at = field_validator("ended_at")(_validate_utc_timestamp)


@router.patch("/v1/events/{event_id}", tags=["events"])
def patch_event(event_id: str, body: EventPatch, request: Request, db: Session = Depends(get_db),
                c: Container = Depends(container), p: Principal = Depends(require("event:write"))) -> dict:
    if not body.model_fields_set:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "", "code": "min_properties",
                                                        "message": "바꿀 항목이 필요합니다"}])
    e = load_scoped(db, c, request, Event, event_id, p)
    if body.human_verified is not None:
        if body.human_verified is not True:
            raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/human_verified", "code": "const",
                                                            "message": "확인 취소는 새 이벤트로 기록합니다"}])
        if not p.user_id:  # 사람만 확인할 수 있다 (요구사항 7.3)
            raise ProblemError("FORBIDDEN", detail="사람 사용자만 확인 표시를 할 수 있습니다")
        if not e.human_verified:
            e.human_verified, e.verified_by, e.verified_at = True, p.user_id, c.clock.now()
            c.audit.record("EVENT_VERIFIED", principal=p, organization_id=e.organization_id, target_type="Event",
                           target_id=e.event_id, source_ip=client_ip(request))
    if body.ended_at is not None:
        end = parse_rfc3339(body.ended_at)
        if end < e.started_at:
            raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/ended_at", "code": "before_start",
                                                            "message": "종료가 시작보다 빠릅니다"}])
        e.ended_at = end
    if body.note is not None:
        e.note = body.note
    if body.add_linked_asset_ids:
        _link_assets(db, c, request, p, e, body.add_linked_asset_ids)
    db.flush()
    return event_view(db, e)
