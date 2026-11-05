"""장비 정책 초안 — 온보딩 마법사 (09번 요구사항 3.2·3.3, ADR-0037). 계약: contracts/openapi.yaml /v1/policy-drafts.

- 만들기·고치기: ORG_ADMIN(자기 기관)·PLATFORM_OPERATOR (policy_draft:write). 초안은 아무것도 바꾸지 않는다.
- 적용·반려: PLATFORM_OPERATOR + 다중 인증 (device_policy:write). ORG_ADMIN은 자기 초안을 적용할 수 없다.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.context import ID_PATTERN
from app.api.deps import DB, client_ip, container, load_scoped, page_params, require
from app.api.onboarding import load_card
from app.api.pagination import paginate
from app.container import Container
from app.db.models import Device, PolicyDraft
from app.domain.principal import Principal
from app.errors import ProblemError
from app.services import policy_drafts
from app.services.tenant import scoped_select

router = APIRouter(tags=["context"])
FieldName = Literal["collection_mode", "allowed_data_roles", "local_copy_policy", "local_retention_days",
                    "expected_interval_s", "transfer_windows", "source_timezone"]


class DraftCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str = Field(pattern=ID_PATTERN)
    integration_card_id: str | None = Field(default=None, pattern=ID_PATTERN)
    integration_card_version: int | None = Field(default=None, ge=1, strict=True)
    values: dict[str, Any] = Field(default_factory=dict)  # 항목별 형식은 장비 정책 계약으로 (input_errors)
    needs_confirmation: list[FieldName] = Field(default_factory=list, max_length=7)
    note: str = Field(default="", max_length=1000)


class DraftPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1, strict=True)
    values: dict[str, Any] = Field(default_factory=dict)
    needs_confirmation: list[FieldName] = Field(default_factory=list, max_length=7)
    note: str | None = Field(default=None, max_length=1000)


class DraftApply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1, strict=True)
    expected_policy_version: int | None = Field(ge=1, strict=True)  # 필수, 저장본이 없었으면 null
    reason: str | None = Field(default=None, min_length=2, max_length=500)


class DraftReject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=2, max_length=500)


def _inputs(values: dict[str, Any], unset: list[str]) -> None:
    errors = policy_drafts.input_errors(values, unset)
    if errors:
        raise ProblemError("VALIDATION_ERROR", errors=errors)


def _load(db: Session, c: Container, request: Request, p: Principal, draft_id: str, *, lock: bool) -> PolicyDraft:
    if lock:  # 읽기 전에 행을 잠가 상태·revision 확인과 변경 사이에 끼어들지 못하게 한다
        db.execute(select(PolicyDraft.policy_draft_id).where(PolicyDraft.policy_draft_id == draft_id)
                   .with_for_update())
    return load_scoped(db, c, request, PolicyDraft, draft_id, p)


def _conflict(exc: policy_drafts.DraftConflict) -> ProblemError:
    return ProblemError("CONFLICT", detail=str(exc))


@router.get("/v1/policy-drafts")
def list_drafts(status: str | None = None, device_id: str | None = None, integration_card_id: str | None = None,
                db: Session = DB, p: Principal = Depends(require("policy_draft:read")),
                page: dict = Depends(page_params)) -> dict:
    stmt = scoped_select(PolicyDraft, p)  # 조건 형식(열거값·ID)은 계약으로 이미 검사했다 (contract_query)
    if status:
        stmt = stmt.where(PolicyDraft.status == status)
    if device_id:
        stmt = stmt.where(PolicyDraft.device_id == device_id)
    if integration_card_id:
        stmt = stmt.where(PolicyDraft.integration_card_id == integration_card_id)
    rows, nxt = paginate(db, stmt, PolicyDraft.policy_draft_id, page["limit"], page["cursor"])
    return {"items": [policy_drafts.view(db, d, with_preview=False) for d in rows], "next_cursor": nxt}


@router.post("/v1/policy-drafts", status_code=201)
def create_draft(body: DraftCreate, request: Request, db: Session = DB,
                 c: Container = Depends(container), p: Principal = Depends(require("policy_draft:write"))) -> dict:
    _inputs(body.values, list(body.needs_confirmation))
    if body.integration_card_version is not None and body.integration_card_id is None:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/integration_card_version", "code": "dependency",
                                                        "message": "integration_card_id와 함께 보냅니다"}])
    device = load_scoped(db, c, request, Device, body.device_id, p)
    card = load_card(db, c, request, p, body.integration_card_id, body.integration_card_version) \
        if body.integration_card_id else None
    try:
        d = policy_drafts.create(db, device=device, card=card, values=body.values,
                                 unset=list(body.needs_confirmation), note=body.note or None, by=p.principal_id,
                                 now=c.clock.now())
    except policy_drafts.DraftConflict as exc:
        raise _conflict(exc) from None
    c.audit.record("POLICY_DRAFT_CREATED", principal=p, organization_id=d.organization_id, target_type="PolicyDraft",
                   target_id=d.policy_draft_id, source_ip=client_ip(request), connection=db.connection(),
                   details={"device_id": d.device_id, "collector_device_id": d.collector_device_id,
                            "integration_card_id": d.integration_card_id,
                            "integration_card_version": d.integration_card_version,
                            "fields": {k: v["status"] for k, v in d.fields.items()}})
    return policy_drafts.view(db, d, with_preview=True)


@router.get("/v1/policy-drafts/{policy_draft_id}")
def get_draft(policy_draft_id: str, request: Request, db: Session = DB,
              c: Container = Depends(container), p: Principal = Depends(require("policy_draft:read"))) -> dict:
    return policy_drafts.view(db, _load(db, c, request, p, policy_draft_id, lock=False), with_preview=True)


@router.patch("/v1/policy-drafts/{policy_draft_id}")
def update_draft(policy_draft_id: str, body: DraftPatch, request: Request, db: Session = DB,
                 c: Container = Depends(container), p: Principal = Depends(require("policy_draft:write"))) -> dict:
    _inputs(body.values, list(body.needs_confirmation))
    d = _load(db, c, request, p, policy_draft_id, lock=True)
    before = d.revision
    try:
        changes = policy_drafts.update(d, revision=body.revision, values=body.values,
                                       unset=list(body.needs_confirmation), note=body.note,
                                       note_given="note" in body.model_fields_set, by=p.principal_id,
                                       now=c.clock.now())
    except policy_drafts.DraftConflict as exc:
        raise _conflict(exc) from None
    if changes:  # 바뀐 항목의 전후 (메모는 길이만)
        c.audit.record("POLICY_DRAFT_UPDATED", principal=p, organization_id=d.organization_id,
                       target_type="PolicyDraft", target_id=d.policy_draft_id, source_ip=client_ip(request),
                       connection=db.connection(),
                       details={"revision": {"before": before, "after": d.revision}, "changes": changes})
    db.flush()
    return policy_drafts.view(db, d, with_preview=True)


@router.post("/v1/policy-drafts/{policy_draft_id}/apply")
def apply_draft(policy_draft_id: str, body: DraftApply, request: Request, db: Session = DB,
                c: Container = Depends(container), p: Principal = Depends(require("device_policy:write"))) -> dict:
    d = _load(db, c, request, p, policy_draft_id, lock=True)
    try:  # 검사(계약·매핑·같은 기관 카드)에 걸리면 RuleViolation → 422, 아무것도 저장하지 않는다
        result = policy_drafts.apply(db, d, revision=body.revision,
                                     expected_policy_version=body.expected_policy_version, reason=body.reason,
                                     by=p.principal_id, now=c.clock.now())
    except policy_drafts.DraftConflict as exc:
        raise _conflict(exc) from None
    ip, conn = client_ip(request), db.connection()
    c.audit.record("POLICY_CHANGED", principal=p, organization_id=d.organization_id, target_type="Device",
                   target_id=d.collector_device_id, reason=body.reason, source_ip=ip, connection=conn,
                   details={"version": result["device_policy_version"], "policy_version": result["policy_version"],
                            "previous_version": result["previous_policy_version"],
                            "integration_card_id": d.integration_card_id, "policy_draft_id": d.policy_draft_id,
                            "device_id": d.device_id, "source_in_current_policy": result["source_in_current_policy"],
                            "before": result["before"], "after": result["after"],
                            "device_changes": result["device_changes"]})
    c.audit.record("POLICY_DRAFT_APPLIED", principal=p, organization_id=d.organization_id, target_type="PolicyDraft",
                   target_id=d.policy_draft_id, reason=body.reason, source_ip=ip, connection=conn,
                   details={"status": {"before": "DRAFT", "after": "APPLIED"}, "revision": d.revision,
                            "device_policy_version": result["device_policy_version"],
                            "changes": result["changes"]})
    copy_change = next((x for x in result["device_changes"] if x["field"] == "local_copy_policy"), None)
    if copy_change:  # 백업 대상·유일본 안내에 영향 (domain.md 9장, ADR-0028과 같은 감사)
        c.audit.record("DEVICE_LOCAL_COPY_POLICY_CHANGED", principal=p, organization_id=d.organization_id,
                       target_type="Device", target_id=d.device_id, source_ip=ip, connection=conn,
                       details={"from": copy_change["before"], "to": copy_change["after"],
                                "policy_draft_id": d.policy_draft_id})
    return policy_drafts.view(db, d, with_preview=False)


@router.post("/v1/policy-drafts/{policy_draft_id}/reject")
def reject_draft(policy_draft_id: str, body: DraftReject, request: Request, db: Session = DB,
                 c: Container = Depends(container), p: Principal = Depends(require("device_policy:write"))) -> dict:
    d = _load(db, c, request, p, policy_draft_id, lock=True)
    try:
        policy_drafts.reject(d, reason=body.reason, by=p.principal_id, now=c.clock.now())
    except policy_drafts.DraftConflict as exc:
        raise _conflict(exc) from None
    db.flush()
    c.audit.record("POLICY_DRAFT_REJECTED", principal=p, organization_id=d.organization_id, target_type="PolicyDraft",
                   target_id=d.policy_draft_id, reason=body.reason, source_ip=client_ip(request),
                   connection=db.connection(),
                   details={"status": {"before": "DRAFT", "after": "REJECTED"}, "device_id": d.device_id,
                            "revision": d.revision})
    return policy_drafts.view(db, d, with_preview=False)
