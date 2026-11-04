"""회차 기록·증빙 승인·지표 정의·보고서 양식·보고서 (10번)."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters import report_renderers as rr
from app.api.deps import client_ip, container, get_db, load_scoped, page_params, require
from app.api.pagination import paginate
from app.container import Container
from app.db.models import (
    Asset,
    MetricDefinition,
    Project,
    Report,
    ReportTemplate,
    ReportVersion,
    SessionRecord,
    SessionRecordVersion,
    TrialSession,
)
from app.domain.ids import new_id
from app.domain.normalize import parse_rfc3339
from app.domain.principal import Principal
from app.errors import ProblemError
from app.services import evidence, metrics, reports
from app.services.idempotency import run_idempotent
from app.services.tenant import scope_filter
from app.timefmt import iso

router = APIRouter()
CODE = r"^[A-Z][A-Z0-9_]{1,59}$"
UTC_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


def _bad(pointer: str, message: str, code: str = "invalid") -> ProblemError:
    return ProblemError("VALIDATION_ERROR", errors=[{"pointer": pointer, "code": code, "message": message}])


# ------------------------------------------------------------------ 회차 기록 (요구사항 1, 2)


class AssetRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["ASSET"]
    asset_id: str = Field(pattern=r"^ast_[0-9A-HJKMNP-TV-Z]{26}$")
    from_offset_s: float | None = Field(None, ge=0)
    to_offset_s: float | None = Field(None, ge=0)


class EventRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["EVENT"]
    event_id: str = Field(pattern=r"^evt_[0-9A-HJKMNP-TV-Z]{26}$")


class ObsRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["OBSERVATION_RANGE"]
    source_system_id: str = Field(pattern=r"^dev_[0-9A-HJKMNP-TV-Z]{26}$")
    metric: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    from_: str = Field(alias="from", pattern=UTC_TS.pattern)
    to: str = Field(pattern=UTC_TS.pattern)


Ref = Annotated[AssetRef | EventRef | ObsRef, Field(discriminator="kind")]


class RecordCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["LOG", "TEST", "NOTE"]
    title: str = Field(min_length=1, max_length=200)
    test_item: str | None = Field(None, max_length=200)
    conditions: str | None = Field(None, max_length=2000)
    result: str | None = Field(None, max_length=4000)
    note: str | None = Field(None, max_length=4000)
    evidence_refs: list[Ref] = Field(default_factory=list, max_length=500)


class RecordUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["LOG", "TEST", "NOTE"] | None = None
    title: str | None = Field(None, min_length=1, max_length=200)
    test_item: str | None = Field(None, max_length=200)
    conditions: str | None = Field(None, max_length=2000)
    result: str | None = Field(None, max_length=4000)
    note: str | None = Field(None, max_length=4000)
    evidence_refs: list[Ref] | None = Field(None, max_length=500)


def _refs(refs: list[Any]) -> list[dict[str, Any]]:
    return [r.model_dump(by_alias=True, exclude_none=True) for r in refs]


def _check(db: Session, org: str, refs: list[dict[str, Any]]) -> list[str]:
    try:
        return evidence.check_refs(db, org, refs)
    except evidence.BadEvidence as exc:
        raise ProblemError("VALIDATION_ERROR", errors=exc.errors) from None


def _record(db: Session, c: Container, request: Request, record_id: str, p: Principal,
            lock: bool = False) -> SessionRecord:
    rec = load_scoped(db, c, request, SessionRecord, record_id, p)
    if lock:
        db.refresh(rec, with_for_update=True)
    return rec


def _version(db: Session, rec: SessionRecord, version: int | None) -> SessionRecordVersion:
    v = db.get(SessionRecordVersion, (rec.record_id, version or rec.latest_version))
    if v is None:
        raise ProblemError("NOT_FOUND")
    return v


@router.get("/v1/sessions/{session_id}/records", tags=["records"])
def list_records(session_id: str, request: Request, db: Session = Depends(get_db), c: Container = Depends(container),
                 p: Principal = Depends(require("record:read"))) -> dict:
    s = load_scoped(db, c, request, TrialSession, session_id, p)
    rows = db.execute(select(SessionRecord).where(SessionRecord.session_id == s.session_id,
                                                  scope_filter(SessionRecord, p))
                      .order_by(SessionRecord.created_at, SessionRecord.record_id)).scalars()
    return {"items": [evidence.record_view(r, _version(db, r, None)) for r in rows]}


@router.post("/v1/sessions/{session_id}/records", tags=["records"], status_code=201)
def create_record(session_id: str, body: RecordCreate, request: Request, db: Session = Depends(get_db),
                  c: Container = Depends(container), p: Principal = Depends(require("record:write")),
                  idempotency_key: str | None = Header(default=None)) -> JSONResponse:
    def create() -> tuple[int, dict]:
        s = load_scoped(db, c, request, TrialSession, session_id, p)
        values = {**body.model_dump(exclude={"evidence_refs"}), "evidence_refs": _refs(body.evidence_refs)}
        assets = _check(db, s.organization_id, values["evidence_refs"])
        now = c.clock.now()
        rec = SessionRecord(record_id=new_id("record"), organization_id=s.organization_id, project_id=s.project_id,
                            session_id=s.session_id, latest_version=0, created_by=p.principal_id, created_at=now,
                            updated_at=now)
        db.add(rec)
        v = evidence.add_version(db, rec, values, p.principal_id, now, assets)
        return 201, evidence.record_view(rec, v)

    status, resp, replayed = run_idempotent(db, c.clock, c.settings.idempotency_retention_s, p,
                                            f"POST /v1/sessions/{session_id}/records", idempotency_key,
                                            body.model_dump(mode="json", by_alias=True), create)
    return JSONResponse(resp, status_code=status, headers={"Idempotent-Replayed": "true"} if replayed else None)


@router.get("/v1/records/{record_id}", tags=["records"])
def get_record(record_id: str, request: Request, version: int | None = Query(None, ge=1),
               db: Session = Depends(get_db), c: Container = Depends(container),
               p: Principal = Depends(require("record:read"))) -> dict:
    rec = _record(db, c, request, record_id, p)
    return evidence.record_view(rec, _version(db, rec, version))


@router.patch("/v1/records/{record_id}", tags=["records"])
def update_record(record_id: str, body: RecordUpdate, request: Request, db: Session = Depends(get_db),
                  c: Container = Depends(container), p: Principal = Depends(require("record:write"))) -> dict:
    """수정은 항상 새 버전 (요구사항 1.3, 2.1). 승인된 버전은 그대로 남는다."""
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise _bad("", "바꿀 값이 없습니다", "minProperties")
    if fields.get("title", "") is None or fields.get("kind", "") is None:
        raise _bad("/title", "제목·종류는 비울 수 없습니다")
    rec = _record(db, c, request, record_id, p, lock=True)
    cur = _version(db, rec, None)
    values = {k: getattr(cur, k) for k in evidence.RECORD_FIELDS}
    values.update(fields)
    if body.evidence_refs is not None:
        values["evidence_refs"] = _refs(body.evidence_refs)
    assets = _check(db, rec.organization_id, values["evidence_refs"])
    v = evidence.add_version(db, rec, values, p.principal_id, c.clock.now(), assets)
    return evidence.record_view(rec, v)


@router.get("/v1/records/{record_id}/versions", tags=["records"])
def record_versions(record_id: str, request: Request, db: Session = Depends(get_db),
                    c: Container = Depends(container), p: Principal = Depends(require("record:read"))) -> dict:
    rec = _record(db, c, request, record_id, p)
    rows = db.execute(select(SessionRecordVersion).where(SessionRecordVersion.record_id == rec.record_id)
                      .order_by(SessionRecordVersion.version)).scalars()
    return {"items": [evidence.record_view(rec, v) for v in rows]}


@router.post("/v1/records/{record_id}/approve", tags=["records"])
def approve_record(record_id: str, request: Request, db: Session = Depends(get_db),
                   c: Container = Depends(container), p: Principal = Depends(require("record:approve"))) -> dict:
    """증빙 승인 (요구사항 2): 최신 버전을 고정, 인용 자산 PROTECTED."""
    rec = _record(db, c, request, record_id, p, lock=True)
    v = _version(db, rec, None)
    if v.status == "APPROVED":
        raise ProblemError("CONFLICT", detail="이미 승인된 버전입니다")
    now = c.clock.now()
    v.status, v.approved_by, v.approved_at = "APPROVED", p.principal_id, now
    protected = evidence.protect(db, evidence.cited_assets(db, "RECORD", rec.record_id, v.version), now)
    c.audit.record("EVIDENCE_APPROVED", principal=p, organization_id=rec.organization_id,
                   target_type="SessionRecord", target_id=rec.record_id, source_ip=client_ip(request),
                   details={"version": v.version, "protected_assets": protected})
    return evidence.record_view(rec, v)


# ------------------------------------------------------------------ 지표 정의·보고서 양식 (요구사항 3.1, 4.6)


class MetricCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(pattern=CODE)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)
    unit: str | None = Field(None, max_length=32)
    kind: Literal["EVENT_COUNT", "EVENT_RATIO", "OBSERVATION_STAT", "VIDEO_HOURS"]
    params: dict[str, Any]


def metric_view(d: MetricDefinition) -> dict[str, Any]:
    return {"code": d.code, "version": d.version, "name": d.name, "description": d.description, "unit": d.unit,
            "kind": d.kind, "params": d.params, "formula": metrics.formula(d.kind, d.params),
            "created_at": iso(d.created_at)}


@router.get("/v1/metric-definitions", tags=["reports"])
def list_metrics(db: Session = Depends(get_db), _: Principal = Depends(require("report:read"))) -> dict:
    rows = db.execute(select(MetricDefinition).order_by(MetricDefinition.code, MetricDefinition.version)).scalars()
    return {"items": [metric_view(d) for d in rows]}


@router.post("/v1/metric-definitions", tags=["reports"], status_code=201)
def create_metric(body: MetricCreate, request: Request, db: Session = Depends(get_db),
                  c: Container = Depends(container),
                  p: Principal = Depends(require("metric_definition:write"))) -> dict:
    errors = metrics.validate(body.kind, body.params)
    if errors:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/params", "code": "invalid", "message": e}
                                                       for e in errors])
    prev = metrics.latest(db, body.code)
    d = MetricDefinition(version=(prev.version + 1) if prev else 1, created_by=p.principal_id,
                         created_at=c.clock.now(), **body.model_dump())
    db.add(d)
    db.flush()
    c.audit.record("METRIC_DEFINITION_CREATED", principal=p, target_type="MetricDefinition",
                   target_id=f"{d.code}:v{d.version}", source_ip=client_ip(request))
    return metric_view(d)


class TemplateField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,59}$")
    label: str = Field(min_length=1, max_length=200)
    auto: str | None = Field(None, max_length=2000)


class TemplateSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["TEXT", "FIELDS", "METRICS", "RECORDS"]
    title: str = Field(min_length=1, max_length=200)
    text: str | None = Field(None, max_length=10000)
    fields: list[TemplateField] | None = None
    metrics: list[Annotated[str, Field(pattern=CODE)]] | None = None


class TemplateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(pattern=CODE)
    name: str = Field(min_length=1, max_length=200)
    scope: Literal["SESSION", "PERIOD"] = "SESSION"
    sections: list[TemplateSection] = Field(min_length=1)


def template_view(t: ReportTemplate) -> dict[str, Any]:
    return {"code": t.code, "version": t.version, "name": t.name, "scope": t.scope, "sections": t.sections,
            "has_hwpx": bool(t.hwpx_key), "hwpx_sha256": t.hwpx_sha256, "created_at": iso(t.created_at)}


@router.get("/v1/report-templates", tags=["reports"])
def list_templates(db: Session = Depends(get_db), _: Principal = Depends(require("report:read"))) -> dict:
    rows = db.execute(select(ReportTemplate).order_by(ReportTemplate.code, ReportTemplate.version)).scalars()
    return {"items": [template_view(t) for t in rows]}


@router.post("/v1/report-templates", tags=["reports"], status_code=201)
def create_template(body: TemplateCreate, request: Request, db: Session = Depends(get_db),
                    c: Container = Depends(container),
                    p: Principal = Depends(require("report_template:write"))) -> dict:
    errors = []
    keys: set[str] = set()
    for i, s in enumerate(body.sections):
        for code in s.metrics or []:
            if metrics.latest(db, code) is None:
                errors.append({"pointer": f"/sections/{i}/metrics", "code": "unknown_metric",
                               "message": f"등록되지 않은 지표입니다: {code}"})
        for f in s.fields or []:
            if f.key in keys:
                errors.append({"pointer": f"/sections/{i}/fields", "code": "duplicate", "message": f.key})
            keys.add(f.key)
    if errors:
        raise ProblemError("VALIDATION_ERROR", errors=errors)
    prev = reports.latest_template(db, body.code)
    t = ReportTemplate(code=body.code, version=(prev.version + 1) if prev else 1, name=body.name, scope=body.scope,
                       sections=[s.model_dump(exclude_none=True) for s in body.sections], created_by=p.principal_id,
                       created_at=c.clock.now())
    db.add(t)
    db.flush()
    c.audit.record("REPORT_TEMPLATE_CREATED", principal=p, target_type="ReportTemplate",
                   target_id=f"{t.code}:v{t.version}", source_ip=client_ip(request))
    return template_view(t)


@router.put("/v1/report-templates/{template_code}/versions/{version}/hwpx", tags=["reports"])
def put_template_hwpx(template_code: str, version: int, request: Request,
                      data: bytes = Body(..., media_type="application/octet-stream"),
                      db: Session = Depends(get_db), c: Container = Depends(container),
                      p: Principal = Depends(require("report_template:write"))) -> dict:
    import hashlib

    t = reports.latest_template(db, template_code, version)
    if t is None:
        raise ProblemError("NOT_FOUND")
    if len(data) > 10 * 1024 * 1024:
        raise ProblemError("PAYLOAD_TOO_LARGE")
    problems = rr.check_hwpx(data)
    if problems:
        raise _bad("", problems[0])
    key = f"ops/report-templates/{t.code}/v{t.version}.hwpx"
    c.storage.put_object(c.settings.bucket_evidence, key, data, rr.MIME["HWPX"])
    t.hwpx_key, t.hwpx_sha256 = key, hashlib.sha256(data).hexdigest()
    c.audit.record("REPORT_TEMPLATE_HWPX_SET", principal=p, target_type="ReportTemplate",
                   target_id=f"{t.code}:v{t.version}", source_ip=client_ip(request), details={"sha256": t.hwpx_sha256})
    return template_view(t)


# ------------------------------------------------------------------ 보고서 (요구사항 4, 5, 7)


class ReportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_code: str = Field(pattern=CODE)
    template_version: int | None = Field(None, ge=1)
    session_id: str | None = None
    project_id: str | None = None
    from_: str | None = Field(None, alias="from")
    to: str | None = None
    title: str | None = Field(None, max_length=200)


def _viewer(p: Principal) -> bool:
    return not p.is_machine and p.has_role("PROGRAM_VIEWER") and not p.has_role("ORG_ADMIN", "ORG_MEMBER",
                                                                                  "PLATFORM_OPERATOR")


def _load_report(db: Session, c: Container, request: Request, report_id: str, p: Principal) -> Report:
    rep = load_scoped(db, c, request, Report, report_id, p)  # 관리기관 열람자는 배정 사업만
    if _viewer(p) and db.execute(select(ReportVersion.version).where(
            ReportVersion.report_id == rep.report_id, reports.submitted_filter()).limit(1)).scalar() is None:
        raise ProblemError("NOT_FOUND")  # 제출본이 없으면 존재를 드러내지 않는다
    return rep


def _report_version(db: Session, rep: Report, p: Principal, version: int | None) -> ReportVersion:
    q = select(ReportVersion).where(ReportVersion.report_id == rep.report_id)
    if _viewer(p):
        q = q.where(reports.submitted_filter())
    q = q.where(ReportVersion.version == version) if version else q.order_by(ReportVersion.version.desc())
    v = db.execute(q.limit(1)).scalar_one_or_none()
    if v is None:
        raise ProblemError("NOT_FOUND")
    return v


@router.get("/v1/reports", tags=["reports"])
def list_reports(session_id: str | None = None, page: dict = Depends(page_params), db: Session = Depends(get_db),
                 p: Principal = Depends(require("report:read"))) -> dict:
    stmt = select(Report).where(scope_filter(Report, p))
    if session_id:
        stmt = stmt.where(Report.session_id == session_id)
    if _viewer(p):
        stmt = stmt.where(Report.report_id.in_(select(ReportVersion.report_id).where(reports.submitted_filter())))
    rows, nxt = paginate(db, stmt, Report.report_id, page["limit"], page["cursor"])
    return {"items": [reports.report_view(r, _report_version(db, r, p, None)) for r in rows], "next_cursor": nxt}


@router.post("/v1/reports", tags=["reports"], status_code=201)
def create_report(body: ReportCreate, request: Request, db: Session = Depends(get_db),
                  c: Container = Depends(container), p: Principal = Depends(require("report:write"))) -> dict:
    tpl = reports.latest_template(db, body.template_code, body.template_version)
    if tpl is None:
        raise _bad("/template_code", "등록되지 않은 보고서 양식입니다")
    now = c.clock.now()
    if tpl.scope == "SESSION":
        if not body.session_id:
            raise _bad("/session_id", "회차 보고서 양식은 session_id가 필요합니다", "required")
        s = load_scoped(db, c, request, TrialSession, body.session_id, p)
        project_id, start, end, title = s.project_id, s.starts_at, (s.ends_at or now), f"{tpl.name} — {s.label}"
    else:
        if not (body.project_id and body.from_ and body.to):
            raise _bad("", "기간 보고서 양식은 project_id·from·to가 필요합니다", "required")
        prj = load_scoped(db, c, request, Project, body.project_id, p)
        try:
            start, end = parse_rfc3339(body.from_), parse_rfc3339(body.to)
        except ValueError:
            raise _bad("/from", "RFC 3339 UTC 시각이어야 합니다") from None
        if start >= end:
            raise _bad("/to", "끝이 시작보다 늦어야 합니다")
        project_id, title = prj.project_id, f"{tpl.name} — {start.date()} ~ {end.date()}"
    rep = Report(report_id=new_id("report"), organization_id=p.organization_id, project_id=project_id,
                 session_id=body.session_id if tpl.scope == "SESSION" else None, period_from=start, period_to=end,
                 title=body.title or title, template_code=tpl.code, template_version=tpl.version, latest_version=0,
                 created_by=p.principal_id, created_at=now)
    db.add(rep)
    db.flush()
    v = reports.generate(c, db, rep, tpl, p.principal_id)
    c.audit.record("REPORT_CREATED", principal=p, organization_id=rep.organization_id, target_type="Report",
                   target_id=rep.report_id, source_ip=client_ip(request),
                   details={"template": f"{tpl.code}:v{tpl.version}"})
    return reports.report_view(rep, v)


@router.get("/v1/reports/{report_id}", tags=["reports"])
def get_report(report_id: str, request: Request, version: int | None = Query(None, ge=1),
               db: Session = Depends(get_db), c: Container = Depends(container),
               p: Principal = Depends(require("report:read"))) -> dict:
    rep = _load_report(db, c, request, report_id, p)
    return reports.report_view(rep, _report_version(db, rep, p, version))


def _owned(db: Session, c: Container, request: Request, report_id: str, p: Principal) -> Report:
    rep = _load_report(db, c, request, report_id, p)
    if rep.organization_id != p.organization_id:  # 운영자는 읽기만
        raise ProblemError("FORBIDDEN")
    db.refresh(rep, with_for_update=True)
    return rep


@router.post("/v1/reports/{report_id}/versions", tags=["reports"], status_code=201)
def new_report_version(report_id: str, request: Request, db: Session = Depends(get_db),
                       c: Container = Depends(container), p: Principal = Depends(require("report:write"))) -> dict:
    rep = _owned(db, c, request, report_id, p)
    tpl = reports.latest_template(db, rep.template_code, rep.template_version)
    if tpl is None:
        raise ProblemError("CONFLICT", detail="보고서 양식을 찾을 수 없습니다")
    if rep.session_id:  # 회차가 끝났으면 끝 시각, 진행 중이면 지금까지
        s = db.get(TrialSession, rep.session_id)
        rep.period_to = (s.ends_at if s and s.ends_at else c.clock.now())
    v = reports.generate(c, db, rep, tpl, p.principal_id)
    return reports.report_view(rep, v)


class FieldEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    value: str | None = Field(max_length=10000)


class FieldsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: list[FieldEdit] = Field(min_length=1)


@router.patch("/v1/reports/{report_id}/fields", tags=["reports"])
def edit_report_fields(report_id: str, body: FieldsPatch, request: Request, db: Session = Depends(get_db),
                       c: Container = Depends(container), p: Principal = Depends(require("report:write"))) -> dict:
    rep = _owned(db, c, request, report_id, p)
    v = _report_version(db, rep, p, None)
    if v.status != "DRAFT":
        raise ProblemError("INVALID_STATE_TRANSITION", detail="초안만 고칠 수 있습니다. 새 버전을 만드세요")
    unknown = reports.edit_fields(v, [f.model_dump() for f in body.fields], p.principal_id, c.clock.now())
    if unknown:
        raise _bad("/fields", f"양식에 없는 항목입니다: {', '.join(unknown)}")
    return reports.report_view(rep, v)


@router.post("/v1/reports/{report_id}/approve", tags=["reports"])
def approve_report(report_id: str, request: Request, db: Session = Depends(get_db),
                   c: Container = Depends(container), p: Principal = Depends(require("report:approve"))) -> dict:
    rep = _owned(db, c, request, report_id, p)
    v = _report_version(db, rep, p, None)
    reports.approve(c, db, rep, v, p.principal_id, set(p.roles))
    c.audit.record("REPORT_APPROVED", principal=p, organization_id=rep.organization_id, target_type="Report",
                   target_id=rep.report_id, source_ip=client_ip(request),
                   details={"version": v.version, "sha256": v.snapshot_sha256})
    return reports.report_view(rep, v)


@router.post("/v1/reports/{report_id}/submit", tags=["reports"])
def submit_report(report_id: str, request: Request, db: Session = Depends(get_db),
                  c: Container = Depends(container), p: Principal = Depends(require("report:submit"))) -> dict:
    rep = _owned(db, c, request, report_id, p)
    v = _report_version(db, rep, p, None)
    reports.submit(c, v, p.principal_id, set(p.roles))
    c.audit.record("REPORT_SUBMITTED", principal=p, organization_id=rep.organization_id, target_type="Report",
                   target_id=rep.report_id, source_ip=client_ip(request), details={"version": v.version})
    return reports.report_view(rep, v)


@router.get("/v1/reports/{report_id}/export", tags=["reports"])
def export_report(report_id: str, request: Request, format: Literal["PDF", "XLSX", "HWPX"],  # noqa: A002
                  version: int | None = Query(None, ge=1), db: Session = Depends(get_db),
                  c: Container = Depends(container), p: Principal = Depends(require("report:read"))) -> Response:
    if format not in rr.enabled_formats(c.settings.report_formats):
        raise ProblemError("CONFLICT", detail=f"{format} 내보내기가 켜져 있지 않습니다 (REPORT_FORMATS)")
    rep = _load_report(db, c, request, report_id, p)
    v = _report_version(db, rep, p, version)
    tpl = reports.latest_template(db, rep.template_code, rep.template_version)
    if tpl is None:
        raise ProblemError("CONFLICT", detail="보고서 양식을 찾을 수 없습니다")
    doc = reports.render_model(db, rep, v, tpl)
    if format == "XLSX":
        data = rr.render_xlsx(doc)
    elif format == "PDF":
        data = rr.render_pdf(doc)
    else:
        if not tpl.hwpx_key:
            raise ProblemError("CONFLICT", detail="이 양식에는 HWPX 원본이 없습니다")
        data = rr.render_hwpx(c.storage.get_object(c.settings.bucket_evidence, tpl.hwpx_key), doc)
    c.audit.record("REPORT_EXPORTED", principal=p, organization_id=rep.organization_id, target_type="Report",
                   target_id=rep.report_id, source_ip=client_ip(request),
                   details={"version": v.version, "format": format})
    name = f"{rep.report_id}-v{v.version}.{format.lower()}"
    return Response(data, media_type=rr.MIME[format], headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/v1/reports/{report_id}/metrics/{metric_code}/trace", tags=["reports"])
def trace_metric(report_id: str, metric_code: str, request: Request, version: int | None = Query(None, ge=1),
                 db: Session = Depends(get_db), c: Container = Depends(container),
                 p: Principal = Depends(require("report:read"))) -> dict:
    rep = _load_report(db, c, request, report_id, p)
    v = _report_version(db, rep, p, version)

    def visible(ids: list[str]) -> set[str]:  # 권한 범위 안의 자산만 (관리기관 열람자는 L1 이상)
        return set(db.execute(select(Asset.asset_id).where(Asset.asset_id.in_(ids), scope_filter(Asset, p)))
                   .scalars()) if ids else set()

    out = reports.trace(db, rep, v, metric_code, visible)
    if out is None:
        raise ProblemError("NOT_FOUND")
    return out

