"""Auth_Middleware 핵심: Bearer 토큰 → Principal (02번 요구사항 1·5·6)."""

from __future__ import annotations

import logging

import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ApiKey, Device, DeviceCredential, Organization, ProjectAssignment, User
from app.domain.principal import DEVICE_SCOPES, Principal
from app.ports.clock import ClockPort
from app.ports.identity import IdentityPort, InvalidToken
from app.services.tokens import MachineTokenIssuer

log = logging.getLogger(__name__)


class AuthFailed(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Authenticator:
    def __init__(self, identity: IdentityPort, machine: MachineTokenIssuer, clock: ClockPort) -> None:
        self.identity = identity
        self.machine = machine
        self.clock = clock

    def authenticate(self, db: Session, token: str) -> Principal:
        if self.machine.is_machine_token(token):
            return self._machine(db, token)
        return self._user(db, token)

    def _user(self, db: Session, token: str) -> Principal:
        try:
            claims = self.identity.verify_access_token(token)
        except InvalidToken as exc:
            raise AuthFailed(f"user_token_{exc}") from None
        user = db.execute(select(User).where(User.idp_subject == claims.subject)).scalar_one_or_none()
        if user is None or user.status != "ACTIVE":
            raise AuthFailed("user_inactive_or_unknown")
        # 사용자는 기관 하나에만 속한다. 토큰의 기관 클레임이 있으면 DB와 같아야 한다 (02번 요구사항 1.3)
        if claims.organization_id and claims.organization_id != user.organization_id:
            raise AuthFailed("organization_mismatch")
        org = db.get(Organization, user.organization_id)
        if org is None or org.status != "ACTIVE":
            raise AuthFailed("organization_inactive")
        assigned = frozenset(db.execute(
            select(ProjectAssignment.project_id).where(ProjectAssignment.user_id == user.user_id)
        ).scalars())
        # 역할은 플랫폼 DB가 기준이다 (구성원 관리·감사 기록을 한 곳에서). IdP 역할은 쓰지 않는다.
        roles = frozenset(user.roles)
        return Principal(kind="USER", organization_id=user.organization_id, org_type=org.org_type,
                         roles=roles, mfa=claims.mfa, user_id=user.user_id, assigned_project_ids=assigned)

    def _machine(self, db: Session, token: str) -> Principal:
        now = self.clock.now()
        try:
            c = self.machine.verify(token, now)
        except jwt.PyJWTError:
            raise AuthFailed("machine_token_invalid") from None
        org = db.get(Organization, c.organization_id)
        if org is None or org.status != "ACTIVE":
            raise AuthFailed("organization_inactive")
        if c.kind == "DEVICE":
            cred = db.get(DeviceCredential, c.client_id)
            if cred is None or cred.status != "ACTIVE":  # 폐기 즉시 거부 (02번 요구사항 5.4)
                raise AuthFailed("device_credential_revoked")
            collector = db.get(Device, cred.device_id)
            if collector is None or collector.status != "ACTIVE":
                raise AuthFailed("device_inactive")
            sources = set(db.execute(
                select(Device.device_id).where(Device.collector_device_id == collector.device_id,
                                               Device.organization_id == collector.organization_id,
                                               Device.status != "REVOKED")
            ).scalars())
            sources.add(collector.device_id)  # 수집기가 스스로 원천일 수 있다 (domain.md 1장)
            return Principal(kind="DEVICE", organization_id=cred.organization_id, org_type=org.org_type,
                             roles=frozenset({"DEVICE"}), device_id=collector.device_id, client_id=c.client_id,
                             scopes=c.scopes & DEVICE_SCOPES, mapped_source_ids=frozenset(sources))
        if c.kind == "API_KEY":
            key = db.get(ApiKey, c.subject_id)
            if key is None or key.revoked_at is not None or key.expires_at <= now:
                raise AuthFailed("api_key_revoked_or_expired")  # 즉시 거부 (02번 요구사항 6.2)
            return Principal(kind="API_KEY", organization_id=key.organization_id, org_type=org.org_type,
                             api_key_id=key.api_key_id, client_id=c.client_id,
                             scopes=c.scopes & frozenset(key.scopes))
        raise AuthFailed("unknown_machine_kind")
