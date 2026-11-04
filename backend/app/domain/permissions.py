"""Permission_Table — 역할별 허용 동작을 한 곳에 선언한다 (02번 요구사항 3). 표에 없으면 거부한다.

- roles: 이 동작을 할 수 있는 사용자 역할 (domain.md 2장)
- scope: 기기·API 키 주체가 이 동작을 하려면 가져야 하는 범위 (없으면 기기·API 키 불가)
- mfa: 고위험 동작 — 다중 인증으로 발급된 사용자 토큰만 (02번 요구사항 4.2)
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.principal import Principal

PROVIDER = frozenset({"ORG_ADMIN", "ORG_MEMBER"})
ALL_USER_ROLES = frozenset({"ORG_ADMIN", "ORG_MEMBER", "PROGRAM_VIEWER", "PLATFORM_OPERATOR", "REVIEWER",
                            "CONSUMER"})


@dataclass(frozen=True)
class Rule:
    roles: frozenset[str]
    scope: str | None = None
    mfa: bool = False


PERMISSIONS: dict[str, Rule] = {
    # 01 시스템
    "system:capabilities": Rule(ALL_USER_ROLES, scope="status:read"),
    "me:read": Rule(ALL_USER_ROLES, scope="status:read"),
    # 02 인증·자격증명·감사
    "enrollment_code:create": Rule(frozenset({"ORG_ADMIN"}), mfa=True),
    "device_credential:revoke": Rule(frozenset({"ORG_ADMIN"}), mfa=True),
    "api_key:create": Rule(frozenset({"ORG_ADMIN"}), mfa=True),
    "api_key:read": Rule(frozenset({"ORG_ADMIN"})),
    "api_key:revoke": Rule(frozenset({"ORG_ADMIN"})),
    "audit:read": Rule(frozenset({"ORG_ADMIN", "PLATFORM_OPERATOR"})),
    # 03 맥락 (실증사업·회차·장비)
    "project:read": Rule(PROVIDER | {"PROGRAM_VIEWER", "PLATFORM_OPERATOR"}),
    "session:read": Rule(PROVIDER | {"PROGRAM_VIEWER", "PLATFORM_OPERATOR"}, scope="upload:write"),
    "session:write": Rule(PROVIDER),
    "device:read": Rule(PROVIDER | {"PLATFORM_OPERATOR"}),
    "device:write": Rule(frozenset({"ORG_ADMIN", "PLATFORM_OPERATOR"})),
    "device_policy:read": Rule(PROVIDER | {"PLATFORM_OPERATOR"}, scope="agent:policy"),
    "device_policy:write": Rule(frozenset({"PLATFORM_OPERATOR"}), mfa=True),
    "agent:heartbeat": Rule(frozenset(), scope="agent:heartbeat"),
    "agent_health:read": Rule(PROVIDER | {"PLATFORM_OPERATOR"}, scope="status:read"),
    "schema:read": Rule(ALL_USER_ROLES, scope="status:read"),
    "code:read": Rule(ALL_USER_ROLES, scope="status:read"),
    "contract:validate": Rule(PROVIDER | {"PLATFORM_OPERATOR"}, scope="upload:write"),
    # 04 수집
    "upload:write": Rule(PROVIDER, scope="upload:write"),
    "upload:read": Rule(PROVIDER, scope="upload:write"),
    "asset:read": Rule(PROVIDER | {"PROGRAM_VIEWER", "PLATFORM_OPERATOR"}, scope="asset:read"),
    "asset:receipt": Rule(PROVIDER, scope="upload:write"),
    "webhook:manage": Rule(frozenset({"ORG_ADMIN"}), scope="webhook:manage"),
}


class PermissionDenied(Exception):
    def __init__(self, action: str, mfa_required: bool = False) -> None:
        super().__init__(action)
        self.action = action
        self.mfa_required = mfa_required


def check(principal: Principal, action: str) -> None:
    rule = PERMISSIONS.get(action)
    if rule is None:  # 기본 거부
        raise PermissionDenied(action)
    if principal.is_machine:
        if rule.scope is None or rule.scope not in principal.scopes:
            raise PermissionDenied(action)
        return
    if not principal.roles & rule.roles:
        raise PermissionDenied(action)
    if rule.mfa and not principal.mfa:
        raise PermissionDenied(action, mfa_required=True)


def allowed(principal: Principal, action: str) -> bool:
    try:
        check(principal, action)
        return True
    except PermissionDenied:
        return False
