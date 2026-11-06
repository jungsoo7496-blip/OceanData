"""기기·API 키 인증, 에이전트 등록, 감사 조회, /v1/me (02번 요구사항 1·5·6·7)."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import client_ip, container, get_db, load_scoped, page_params, require
from app.api.pagination import paginate
from app.container import Container
from app.db.models import ApiKey, AuditLog, Device, DeviceCredential, EnrollmentCode
from app.domain.ids import new_id
from app.domain.principal import DEVICE_SCOPES, Principal
from app.errors import ProblemError
from app.ratelimit import Limiter
from app.services.tokens import hash_secret, new_secret, verify_secret
from app.timefmt import iso

router = APIRouter()




_auth_limiter = Limiter(limit=30, window_s=60)  # 토큰·등록 엔드포인트 (무차별 대입 방지)


# ---- 토큰 교환
@router.post("/v1/auth/token", tags=["auth"])
def issue_token(request: Request,
                grant_type: Annotated[Literal["client_credentials"], Form()],
                client_id: Annotated[str, Form(max_length=128)],
                client_secret: Annotated[str, Form(max_length=256)],
                scope: Annotated[str | None, Form()] = None,
                db: Session = Depends(get_db), c: Container = Depends(container)) -> JSONResponse:
    source = client_ip(request)
    _auth_limiter.check(f"token-ip:{source}")
    _auth_limiter.check(f"token-client:[{source}]:{client_id}")
    now = c.clock.now()
    requested = frozenset(scope.split()) if scope else None

    def fail(reason: str) -> ProblemError:
        c.audit.record("AUTH_FAILED", result="DENIED", actor_kind="MACHINE", actor_id=client_id[:64],
                       source_ip=client_ip(request), details={"reason": reason, "endpoint": "token"})
        return ProblemError("UNAUTHENTICATED")

    if client_id.startswith("dcr_"):
        cred = db.get(DeviceCredential, client_id)
        if cred is None or cred.status != "ACTIVE" or not verify_secret(client_secret, cred.secret_hash):
            raise fail("device_credential_invalid")
        allowed = DEVICE_SCOPES
        kind, org, subject = "DEVICE", cred.organization_id, cred.device_id
    elif client_id.startswith("key_"):
        key = db.get(ApiKey, client_id)
        if key is None or key.revoked_at is not None or key.expires_at <= now \
                or not verify_secret(client_secret, key.secret_hash):
            raise fail("api_key_invalid")
        allowed = frozenset(key.scopes)
        kind, org, subject = "API_KEY", key.organization_id, key.api_key_id
    else:
        raise fail("unknown_client")
    if requested is not None and not requested <= allowed:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/scope", "code": "scope_not_allowed",
                                                        "message": "허용되지 않은 범위가 포함되어 있습니다"}])
    token, ttl = c.machine_tokens.issue(kind=kind, client_id=client_id, organization_id=org, subject_id=subject,
                                        scopes=requested if requested is not None else allowed, now=now)
    body = {"access_token": token, "token_type": "Bearer", "expires_in": ttl}  # nosec B105
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


# ---- 등록 코드·에이전트 등록
@router.post("/v1/devices/{device_id}/enrollment-codes", tags=["agents"], status_code=201)
def create_enrollment_code(device_id: str, request: Request, db: Session = Depends(get_db),
                           c: Container = Depends(container),
                           p: Principal = Depends(require("enrollment_code:create"))) -> dict:
    device = load_scoped(db, c, request, Device, device_id, p)
    db.refresh(device, with_for_update={"key_share": True})
    if device.kind != "COLLECTOR":
        raise ProblemError("RULE_VIOLATION", detail="등록 코드는 수집기 장비에만 발급합니다",
                           unmet_conditions=[{"condition": "kind=COLLECTOR", "actual": device.kind}])
    if device.status == "REVOKED":
        raise ProblemError("CONFLICT", detail="폐기된 장비입니다")
    code = secrets.token_urlsafe(18)  # 24자
    now = c.clock.now()
    expires = now + timedelta(seconds=c.settings.enrollment_code_ttl_s)
    db.add(EnrollmentCode(code_hash=hash_secret(code), device_id=device.device_id,
                          organization_id=device.organization_id, expires_at=expires, created_by=p.principal_id,
                          created_at=now))
    db.flush()
    c.audit.record("ENROLLMENT_CODE_ISSUED", principal=p, organization_id=device.organization_id,
                   target_type="Device", target_id=device.device_id, source_ip=client_ip(request),
                   connection=db.connection())
    return {"code": code, "expires_at": iso(expires)}


class EnrollRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enrollment_code: str = Field(min_length=8, max_length=64)
    agent_version: str = Field(max_length=64)
    os: Literal["windows", "linux"]


@router.post("/v1/agents/enroll", tags=["agents"], status_code=201)
def enroll_agent(body: EnrollRequest, request: Request, db: Session = Depends(get_db),
                 c: Container = Depends(container)) -> JSONResponse:
    _auth_limiter.check(f"enroll:{client_ip(request)}")
    code_hash = hash_secret(body.enrollment_code)
    # 발급·등록·폐기는 수집기 행을 먼저 잠근다. 코드는 잠금 획득 뒤 다시 읽어 폐기를 반영한다.
    device = db.execute(
        select(Device).join(EnrollmentCode, EnrollmentCode.device_id == Device.device_id)
        .where(EnrollmentCode.code_hash == code_hash).with_for_update(of=Device, key_share=True)
    ).scalar_one_or_none()
    rec = db.get(EnrollmentCode, code_hash) if device is not None else None
    now = c.clock.now()
    if rec is None or rec.used_at is not None or rec.expires_at <= now:
        reason = "unknown" if rec is None else ("used" if rec.used_at else "expired")
        organization_id = rec.organization_id if rec is not None else None
        db.rollback()  # 성공한 변경이 없으므로 잠금을 놓고 실패 감사를 별도로 기록한다.
        c.audit.record("AGENT_ENROLL_FAILED", result="DENIED", actor_kind="MACHINE", source_ip=client_ip(request),
                       organization_id=organization_id, details={"reason": reason})
        if rec is None:
            raise ProblemError("UNAUTHENTICATED", detail="등록 코드가 올바르지 않습니다")
        raise ProblemError("GONE", detail="이미 사용했거나 만료된 등록 코드입니다" if reason == "used"
                           else "만료된 등록 코드입니다", extra={"reason": reason.upper()})
    if device is None or device.status == "REVOKED":
        raise ProblemError("GONE", detail="폐기된 장비입니다")
    rec.used_at = now
    # 이전 자격증명은 폐기한다 (재등록)
    for old in db.execute(select(DeviceCredential).where(DeviceCredential.device_id == device.device_id,
                                                         DeviceCredential.status == "ACTIVE")).scalars():
        old.status, old.revoked_at = "REVOKED", now
    client_id = "dcr_" + new_id("device")[4:]
    secret = new_secret()
    db.add(DeviceCredential(client_id=client_id, device_id=device.device_id, organization_id=device.organization_id,
                            secret_hash=hash_secret(secret), status="ACTIVE", created_at=now))
    device.status = "ACTIVE"
    db.flush()
    c.audit.record("DEVICE_ENROLLED", actor_kind="DEVICE", actor_id=device.device_id,
                   organization_id=device.organization_id, target_type="Device", target_id=device.device_id,
                   source_ip=client_ip(request), details={"agent_version": body.agent_version, "os": body.os},
                   connection=db.connection())
    resp = {"device_id": device.device_id, "client_id": client_id, "client_secret": secret,
            "token_endpoint": "/v1/auth/token"}  # nosec B105
    return JSONResponse(resp, status_code=201, headers={"Cache-Control": "no-store"})


@router.post("/v1/devices/{device_id}/credentials/revoke", tags=["agents"])
def revoke_device_credentials(device_id: str, request: Request, db: Session = Depends(get_db),
                              c: Container = Depends(container),
                              p: Principal = Depends(require("device_credential:revoke"))) -> dict:
    device = load_scoped(db, c, request, Device, device_id, p)
    db.refresh(device, with_for_update={"key_share": True})
    now = c.clock.now()
    n = 0
    for cred in db.execute(select(DeviceCredential).where(DeviceCredential.device_id == device.device_id,
                                                          DeviceCredential.status == "ACTIVE")).scalars():
        cred.status, cred.revoked_at = "REVOKED", now
        n += 1
    # 폐기 전에 발급했지만 쓰지 않은 등록 코드도 무효로 한다 — 장비가 재등록 대기로 돌아가므로 남은 코드(최대 24시간)로
    # 다시 등록되면 폐기가 무력해진다 (02번 요구사항 5.4). 재등록은 폐기 뒤 새로 발급한 코드로만.
    codes = 0
    for rec in db.execute(select(EnrollmentCode).where(EnrollmentCode.device_id == device.device_id,
                                                       EnrollmentCode.used_at.is_(None),
                                                       EnrollmentCode.expires_at > now)).scalars():
        rec.expires_at = now
        codes += 1
    device.status = "PENDING_ENROLLMENT"
    db.flush()
    c.audit.record("DEVICE_CREDENTIAL_REVOKED", principal=p, organization_id=device.organization_id,
                   target_type="Device", target_id=device.device_id, source_ip=client_ip(request),
                   details={"revoked": n, "enrollment_codes_invalidated": codes}, connection=db.connection())
    return {"device_id": device.device_id, "revoked_credentials": n}


# ---- API 키
class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    scopes: list[Literal["upload:write", "observation:write", "event:write", "asset:read", "status:read",
                         "webhook:manage"]] = Field(min_length=1)
    expires_at: datetime


def _key_view(k: ApiKey) -> dict:
    return {"api_key_id": k.api_key_id, "name": k.name, "scopes": sorted(k.scopes), "expires_at": iso(k.expires_at),
            "revoked_at": iso(k.revoked_at), "created_at": iso(k.created_at)}


@router.post("/v1/api-keys", tags=["auth"], status_code=201)
def create_api_key(body: ApiKeyCreate, request: Request, db: Session = Depends(get_db),
                   c: Container = Depends(container),
                   p: Principal = Depends(require("api_key:create"))) -> JSONResponse:
    now = c.clock.now()
    if body.expires_at.tzinfo is None or body.expires_at <= now:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/expires_at", "code": "invalid",
                                                        "message": "만료일은 시간대가 있는 미래 시각이어야 합니다"}])
    secret = new_secret()
    key = ApiKey(api_key_id=new_id("api_key"), organization_id=p.organization_id, name=body.name,
                 secret_hash=hash_secret(secret), scopes=sorted(set(body.scopes)), expires_at=body.expires_at,
                 created_by=p.principal_id, created_at=now)
    db.add(key)
    c.audit.record("API_KEY_ISSUED", principal=p, organization_id=p.organization_id, target_type="ApiKey",
                   target_id=key.api_key_id, source_ip=client_ip(request), details={"scopes": key.scopes})
    return JSONResponse({**_key_view(key), "client_id": key.api_key_id, "client_secret": secret},
                        status_code=201, headers={"Cache-Control": "no-store"})


@router.get("/v1/api-keys", tags=["auth"])
def list_api_keys(db: Session = Depends(get_db), p: Principal = Depends(require("api_key:read")),
                  page: dict = Depends(page_params)) -> dict:
    stmt = select(ApiKey).where(ApiKey.organization_id == p.organization_id)
    rows, nxt = paginate(db, stmt, ApiKey.api_key_id, page["limit"], page["cursor"])
    return {"items": [_key_view(k) for k in rows], "next_cursor": nxt}


@router.post("/v1/api-keys/{api_key_id}/revoke", tags=["auth"])
def revoke_api_key(api_key_id: str, request: Request, db: Session = Depends(get_db),
                   c: Container = Depends(container), p: Principal = Depends(require("api_key:revoke"))) -> dict:
    key = load_scoped(db, c, request, ApiKey, api_key_id, p)
    if key.revoked_at is None:
        key.revoked_at = c.clock.now()
        c.audit.record("API_KEY_REVOKED", principal=p, organization_id=key.organization_id, target_type="ApiKey",
                       target_id=key.api_key_id, source_ip=client_ip(request))
    return _key_view(key)


# ---- 감사 기록 조회
@router.get("/v1/audit-logs", tags=["auth"])
def list_audit_logs(db: Session = Depends(get_db), p: Principal = Depends(require("audit:read")),
                    page: dict = Depends(page_params)) -> dict:
    stmt = select(AuditLog)
    if not p.has_role("PLATFORM_OPERATOR"):  # ORG_ADMIN: 자기 기관 데이터 관련 기록만 (02번 요구사항 7.5)
        stmt = stmt.where(or_(AuditLog.organization_id == p.organization_id,
                              AuditLog.actor_organization_id == p.organization_id))
    rows, nxt = paginate(db, stmt, AuditLog.audit_id, page["limit"], page["cursor"])
    return {"items": [{"audit_id": r.audit_id, "occurred_at": iso(r.occurred_at), "actor_kind": r.actor_kind,
                       "actor_id": r.actor_id, "action": r.action, "target_type": r.target_type,
                       "target_id": r.target_id, "result": r.result, "request_id": r.request_id,
                       "reason": r.reason} for r in rows], "next_cursor": nxt}


# ---- 현재 주체
@router.get("/v1/me", tags=["context"])
def me(p: Principal = Depends(require("me:read"))) -> dict:
    out: dict = {"principal_kind": p.kind, "organization_id": p.organization_id, "org_type": p.org_type,
                 "roles": sorted(r for r in p.roles), "mfa": p.mfa}
    if p.user_id:
        out["user_id"] = p.user_id
    if p.device_id:
        out["device_id"] = p.device_id
    return out
