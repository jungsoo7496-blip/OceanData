"""보고서 (10번 요구사항 4, 5, 7): 양식으로 초안 생성, 자동값·수정값 구분, 승인 고정·제출, 지표 근거 역추적."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.adapters import report_renderers as rr
from app.db.models import (
    Asset,
    AssetLineage,
    Event,
    EventAsset,
    Organization,
    OrgQuota,
    Project,
    Report,
    ReportTemplate,
    ReportVersion,
    SessionRecord,
    SessionRecordVersion,
    TrialSession,
)
from app.domain import states
from app.domain.errors import RuleViolation, UnmetCondition
from app.services import evidence, metrics
from app.timefmt import iso

if TYPE_CHECKING:
    from app.container import Container

KST = ZoneInfo("Asia/Seoul")
VAR = re.compile(r"\{\{\s*([a-z0-9_.]+)\s*\}\}")


def kst(dt: datetime | None) -> str:
    return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M KST") if dt else ""


def latest_template(db: Session, code: str, version: int | None = None) -> ReportTemplate | None:
    q = select(ReportTemplate).where(ReportTemplate.code == code)
    q = q.where(ReportTemplate.version == version) if version else q.order_by(ReportTemplate.version.desc())
    return db.execute(q.limit(1)).scalar_one_or_none()


def scope_of(rep: Report) -> metrics.Scope:
    start, end = rep.period_from, rep.period_to
    return metrics.Scope(rep.organization_id, rep.project_id, rep.session_id, start, end)  # type: ignore[arg-type]


def variables(db: Session, rep: Report) -> dict[str, str]:
    org = db.get(Organization, rep.organization_id)
    prj = db.get(Project, rep.project_id)
    v = {"organization.name": org.name if org else "", "project.name": prj.name if prj else "",
         "period.from": kst(rep.period_from), "period.to": kst(rep.period_to), "report.title": rep.title}
    if rep.session_id:
        s = db.get(TrialSession, rep.session_id)
        if s:
            v.update({"session.label": s.label, "session.title": s.title, "session.starts_at": kst(s.starts_at),
                      "session.ends_at": kst(s.ends_at), "session.site": s.site or ""})
    return v


def substitute(text: str, values: dict[str, str]) -> str:
    return VAR.sub(lambda m: values.get(m.group(1), "(값 없음)"), text)


def approved_records(db: Session, rep: Report) -> list[tuple[SessionRecord, SessionRecordVersion]]:
    """보고서 범위의 회차 기록 중 승인된 최신 버전."""
    q = select(SessionRecord).where(SessionRecord.organization_id == rep.organization_id)
    if rep.session_id:
        q = q.where(SessionRecord.session_id == rep.session_id)
    else:
        q = q.join(TrialSession, TrialSession.session_id == SessionRecord.session_id).where(
            SessionRecord.project_id == rep.project_id, TrialSession.starts_at < rep.period_to,
            TrialSession.starts_at >= rep.period_from)
    out = []
    for rec in db.execute(q.order_by(SessionRecord.created_at, SessionRecord.record_id)).scalars():
        v = db.execute(select(SessionRecordVersion).where(
            SessionRecordVersion.record_id == rec.record_id, SessionRecordVersion.status == "APPROVED")
            .order_by(SessionRecordVersion.version.desc()).limit(1)).scalar_one_or_none()
        if v is not None:
            out.append((rec, v))
    return out


def generate(c: Container, db: Session, rep: Report, tpl: ReportTemplate, by: str) -> ReportVersion:
    """양식으로 새 초안 버전을 만든다 (요구사항 4.1, 4.2). 지표는 지금 데이터로 계산하고 근거를 함께 저장한다."""
    now = c.clock.now()
    values = variables(db, rep)
    fields, results = [], []
    for s in tpl.sections:
        for f in s.get("fields", []) if s["kind"] == "FIELDS" else []:
            auto = substitute(f.get("auto", ""), values) or None
            fields.append({"key": f["key"], "label": f["label"], "auto_value": auto, "value": auto, "edited": False,
                           "history": []})
        for code in s.get("metrics", []) if s["kind"] == "METRICS" else []:
            d = metrics.latest(db, code)
            if d is None:  # 양식 등록 때 막지만, 정의가 지워진 경우에도 값을 만들지 않는다
                results.append({"metric_code": code, "metric_version": 0, "name": code, "unit": None,
                                "status": "NO_DATA", "value": None, "formula": "정의 없음", "input_count": 0,
                                "missing_count": 0, "sources": {"event_ids": [], "asset_ids": [],
                                                                "observation_ranges": []}, "computed_at": iso(now)})
                continue
            results.append(metrics.compute(db, d, scope_of(rep), now))
    records = approved_records(db, rep)
    rep.latest_version += 1
    v = ReportVersion(report_id=rep.report_id, version=rep.latest_version, status="DRAFT", generated_at=now,
                      data_as_of=now, variables=values, fields=fields, metrics=results,
                      record_refs=[{"record_id": r.record_id, "version": rv.version} for r, rv in records],
                      evidence_damaged=False, damage=[], created_by=by, created_at=now)
    db.add(v)
    db.flush()
    cited = {a for m in results for a in m["sources"]["asset_ids"]}
    for r, rv in records:
        cited |= set(evidence.cited_assets(db, "RECORD", r.record_id, rv.version))
    evidence.cite(db, rep.organization_id, "REPORT", rep.report_id, v.version, sorted(cited), now)
    return v


def edit_fields(v: ReportVersion, edits: list[dict[str, Any]], by: str, now: datetime) -> list[str]:
    """초안 입력 항목 수정. 자동값은 그대로 두고 수정값·수정자·시각·이전 값을 남긴다 (요구사항 4.3)."""
    fields = [dict(f) for f in v.fields]
    index = {f["key"]: f for f in fields}
    unknown = [e["key"] for e in edits if e["key"] not in index]
    if unknown:
        return unknown
    for e in edits:
        f = index[e["key"]]
        new = e["value"] if e["value"] is not None else f["auto_value"]
        if new == f["value"]:
            continue
        f["history"] = [*f.get("history", []), {"previous": f["value"], "value": new, "edited_by": by,
                                                "edited_at": iso(now)}]
        f["value"], f["edited"] = new, new != f["auto_value"]
    v.fields = fields
    return []


def report_view(rep: Report, v: ReportVersion) -> dict[str, Any]:
    return {"report_id": rep.report_id, "organization_id": rep.organization_id, "project_id": rep.project_id,
            "session_id": rep.session_id, "period_from": iso(rep.period_from), "period_to": iso(rep.period_to),
            "title": rep.title, "template_code": rep.template_code, "template_version": rep.template_version,
            "version": v.version, "latest_version": rep.latest_version, "status": v.status,
            "generated_at": iso(v.generated_at), "data_as_of": iso(v.data_as_of), "fields": v.fields,
            "metrics": v.metrics, "record_ids": [r["record_id"] for r in v.record_refs],
            "evidence_damaged": v.evidence_damaged, "damage": v.damage, "approved_by": v.approved_by,
            "approved_at": iso(v.approved_at), "snapshot_sha256": v.snapshot_sha256,
            "submitted_at": iso(v.submitted_at), "created_at": iso(rep.created_at)}


def render_model(db: Session, rep: Report, v: ReportVersion, tpl: ReportTemplate) -> dict[str, Any]:
    """렌더러에 넘길 모델 (양식 순서대로 절을 채운다)."""
    fields = {f["key"]: f for f in v.fields}
    by_code = {m["metric_code"]: m for m in v.metrics}
    records = []
    for ref in v.record_refs:
        rv = db.get(SessionRecordVersion, (ref["record_id"], ref["version"]))
        if rv:
            records.append({"record_id": rv.record_id, "version": rv.version, "title": rv.title,
                            "test_item": rv.test_item, "conditions": rv.conditions, "result": rv.result,
                            "evidence_count": len(rv.evidence_refs or [])})
    sections = []
    for s in tpl.sections:
        sec: dict[str, Any] = {"kind": s["kind"], "title": s["title"]}
        if s["kind"] == "TEXT":
            sec["text"] = substitute(s.get("text", ""), v.variables)
        elif s["kind"] == "FIELDS":
            sec["fields"] = [fields[f["key"]] for f in s.get("fields", []) if f["key"] in fields]
        elif s["kind"] == "METRICS":
            sec["metrics"] = [by_code[c] for c in s.get("metrics", []) if c in by_code]
        else:
            sec["records"] = records
        sections.append(sec)
    damage = None
    if v.evidence_damaged:
        damage = "근거 손상: " + ", ".join(sorted({d["asset_id"] for d in v.damage}))
    return {"title": rep.title, "status_label": rr.STATUS_LABEL[v.status], "version": v.version,
            "generated_at": kst(v.generated_at), "data_as_of": kst(v.data_as_of), "variables": v.variables,
            "sections": sections, "damage_note": damage}


def approve(c: Container, db: Session, rep: Report, v: ReportVersion, by: str, roles: set[str]) -> None:
    """DRAFT → APPROVED: 스냅숏을 EVIDENCE 존에 고정하고 sha256 기록, 인용 자산 PROTECTED, 이전 승인본 SUPERSEDED."""
    now = c.clock.now()
    v.status = states.transition("report_status", v.status, "APPROVED", roles)
    v.approved_by, v.approved_at = by, now
    for old in db.execute(select(ReportVersion).where(ReportVersion.report_id == rep.report_id,
                                                      ReportVersion.version != v.version,
                                                      ReportVersion.status.in_(["APPROVED", "SUBMITTED"]))).scalars():
        old.status = states.transition("report_status", old.status, "SUPERSEDED", {"SERVICE"})
    snapshot = json.dumps(report_view(rep, v), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    key = f"{rep.organization_id}/reports/{rep.report_id}/v{v.version}.json"
    c.storage.put_object(c.settings.bucket_evidence, key, snapshot, "application/json")
    v.snapshot_key, v.snapshot_sha256 = key, hashlib.sha256(snapshot).hexdigest()
    q = db.execute(select(OrgQuota).where(OrgQuota.organization_id == rep.organization_id).with_for_update()) \
        .scalar_one_or_none()
    if q is not None:  # EVIDENCE 존 객체도 기관 사용량 (domain.md 9장)
        q.used_bytes += len(snapshot)
    evidence.protect(db, evidence.cited_assets(db, "REPORT", rep.report_id, v.version), now)


def submit(c: Container, v: ReportVersion, by: str, roles: set[str]) -> None:
    """APPROVED → SUBMITTED. 근거 손상 표시가 있으면 거부하고 원인 자산을 알려준다 (요구사항 5.4, 6.2)."""
    if v.status == "APPROVED" and v.evidence_damaged:
        raise RuleViolation("근거 손상 표시가 있어 제출할 수 없습니다. 근거를 바꾼 새 버전을 승인하세요",
                            [UnmetCondition(condition="근거 손상 없음", actual=f"{d['asset_id']} {d['reason']}")
                             for d in v.damage])
    v.status = states.transition("report_status", v.status, "SUBMITTED", roles)
    v.submitted_by, v.submitted_at = by, c.clock.now()


def trace(db: Session, rep: Report, v: ReportVersion, code: str, visible: Any) -> dict[str, Any] | None:
    """지표 → 회차 기록 → 이벤트·관측값 → 자산 → 원본 (요구사항 7.1, T06-a). visible(ids)에 없는 자산은 숨기고 센다."""
    m = next((x for x in v.metrics if x["metric_code"] == code), None)
    if m is None:
        return None
    src = m["sources"]
    event_ids = set(src.get("event_ids", []))
    events = list(db.execute(select(Event).where(Event.event_id.in_(event_ids))).scalars()) if event_ids else []
    ev_assets: dict[str, list[str]] = {}
    for eid, aid in db.execute(select(EventAsset.event_id, EventAsset.asset_id)
                               .where(EventAsset.event_id.in_(event_ids))).all() if event_ids else []:
        ev_assets.setdefault(eid, []).append(aid)
    asset_ids = set(src.get("asset_ids", [])) | {a for xs in ev_assets.values() for a in xs}
    ranges = src.get("observation_ranges", [])
    records = []
    for ref in v.record_refs:
        rv = db.get(SessionRecordVersion, (ref["record_id"], ref["version"]))
        if rv is None:
            continue
        refs = rv.evidence_refs or []
        hit = any((r["kind"] == "EVENT" and r["event_id"] in event_ids) or
                  (r["kind"] == "ASSET" and r["asset_id"] in asset_ids) or
                  (r["kind"] == "OBSERVATION_RANGE" and any(
                      g["source_system_id"] == r["source_system_id"] and g["metric"] == r["metric"] for g in ranges))
                  for r in refs)
        if hit:
            records.append({"record_id": rv.record_id, "version": rv.version, "title": rv.title})
    shown = visible(sorted(asset_ids))
    assets_out = [{"asset_id": a.asset_id, "variant": a.variant, "ingest_status": a.ingest_status,
                   "originals": originals(db, a.asset_id)}
                  for a in (db.execute(select(Asset).where(Asset.asset_id.in_(shown))).scalars() if shown else [])]
    hidden = len(asset_ids - shown)
    return {"metric": m, "records": records,
            "events": [{"event_id": e.event_id, "event_code": e.event_code, "source": e.source,
                        "human_verified": e.human_verified, "asset_ids": sorted(ev_assets.get(e.event_id, []))}
                       for e in events],
            "observation_ranges": ranges, "assets": sorted(assets_out, key=lambda x: x["asset_id"]),
            "hidden_count": hidden}


def originals(db: Session, asset_id: str) -> list[str]:
    """계보를 거슬러 올라가 부모가 없는 원 자산 (자기 자신이 원본이면 자신)."""
    seen, frontier, roots = {asset_id}, [asset_id], set()
    while frontier:
        parents = db.execute(select(AssetLineage.child_asset_id, AssetLineage.parent_asset_id)
                             .where(AssetLineage.child_asset_id.in_(frontier))).all()
        has_parent = {ch for ch, _ in parents}
        roots |= {x for x in frontier if x not in has_parent}
        frontier = [p for _, p in parents if p not in seen]
        seen |= set(frontier)
    return sorted(roots)


def submitted_filter() -> Any:
    return and_(ReportVersion.submitted_at.is_not(None))
