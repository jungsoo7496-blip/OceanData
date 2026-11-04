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
    "asset:write": Rule(PROVIDER),
    "asset:download": Rule(PROVIDER),  # 운영자 원본 열람은 긴급 열람(02번 요구사항 8)으로만, 관리기관 열람자는 불가
    "asset:reprocess": Rule(frozenset({"PLATFORM_OPERATOR"}), mfa=True),
    "webhook:manage": Rule(frozenset({"ORG_ADMIN"}), scope="webhook:manage"),
    # 08 저장·보존
    "usage:read": Rule(PROVIDER | {"PLATFORM_OPERATOR"}, scope="status:read"),
    "retention:run": Rule(frozenset({"PLATFORM_OPERATOR"})),
    "legal_hold:set": Rule(frozenset({"PLATFORM_OPERATOR"}), mfa=True),
    "asset:delete": Rule(frozenset({"ORG_ADMIN"}), mfa=True),
    "deletion_certificate:read": Rule(frozenset({"ORG_ADMIN", "PLATFORM_OPERATOR"})),
    # 07 센서·이벤트
    "observation:write": Rule(PROVIDER, scope="observation:write"),
    "observation:read": Rule(PROVIDER | {"PLATFORM_OPERATOR"}, scope="status:read"),
    "observation:correct": Rule(frozenset({"ORG_ADMIN"})),
    "mapping:read": Rule(PROVIDER | {"PLATFORM_OPERATOR"}, scope="agent:policy"),
    "mapping:write": Rule(frozenset({"ORG_ADMIN", "PLATFORM_OPERATOR"})),
    "event:write": Rule(PROVIDER, scope="event:write"),
    "event:read": Rule(PROVIDER | {"PLATFORM_OPERATOR"}, scope="status:read"),
    # 10 실증 기록·증빙·보고서
    "record:read": Rule(PROVIDER | {"PLATFORM_OPERATOR"}, scope="status:read"),
    "record:write": Rule(PROVIDER, scope="upload:write"),
    "record:approve": Rule(frozenset({"ORG_ADMIN"})),
    "report:read": Rule(PROVIDER | {"PROGRAM_VIEWER", "PLATFORM_OPERATOR"}, scope="status:read"),
    "report:write": Rule(PROVIDER),
    "report:approve": Rule(frozenset({"ORG_ADMIN"})),
    "report:submit": Rule(frozenset({"ORG_ADMIN"})),
    "metric_definition:write": Rule(frozenset({"PLATFORM_OPERATOR"})),
    "report_template:write": Rule(frozenset({"PLATFORM_OPERATOR"})),
    # 13 공유 등급·데이터셋·배포
    "share:read": Rule(PROVIDER | {"PLATFORM_OPERATOR", "REVIEWER"}),
    "share:candidate": Rule(PROVIDER),
    "share:request": Rule(frozenset({"ORG_ADMIN"})),
    "share:review": Rule(PROVIDER | {"PLATFORM_OPERATOR", "REVIEWER"}),
    "share:decide": Rule(frozenset({"PLATFORM_OPERATOR"})),
    "asset:classify": Rule(frozenset({"PLATFORM_OPERATOR"})),
    "dataset:write": Rule(frozenset({"PLATFORM_OPERATOR"})),
    "dataset:read": Rule(PROVIDER | {"PLATFORM_OPERATOR", "CONSUMER"}),
    "access_request:create": Rule(frozenset({"CONSUMER"})),
    "access_request:read": Rule(PROVIDER | {"PLATFORM_OPERATOR", "CONSUMER"}),
    "access_request:decide": Rule(frozenset({"ORG_ADMIN", "PLATFORM_OPERATOR"})),
    "grant:read": Rule(PROVIDER | {"PLATFORM_OPERATOR", "CONSUMER"}),
    "grant:revoke": Rule(frozenset({"ORG_ADMIN", "PLATFORM_OPERATOR"})),
    "grant:confirm_destruction": Rule(frozenset({"CONSUMER"})),
    "distribution:create": Rule(frozenset({"CONSUMER"})),
    "usage_history:read": Rule(PROVIDER),
    # 14 비식별·검수·태깅
    "deid_review:read": Rule(frozenset({"REVIEWER", "PLATFORM_OPERATOR"})),
    "deid_review:decide": Rule(frozenset({"REVIEWER", "PLATFORM_OPERATOR"})),
    "deid_quality:read": Rule(frozenset({"PLATFORM_OPERATOR", "ORG_ADMIN"})),
    # 12 공공 Open API
    "external_source:read": Rule(frozenset({"PLATFORM_OPERATOR"})),
    "external_source:write": Rule(frozenset({"PLATFORM_OPERATOR"})),
    # 11 수집 모니터링
    "ingest_status:read": Rule(PROVIDER | {"PLATFORM_OPERATOR"}, scope="status:read"),
    "incident:read": Rule(PROVIDER | {"PLATFORM_OPERATOR"}, scope="status:read"),
    "incident:update": Rule(frozenset({"ORG_ADMIN", "PLATFORM_OPERATOR"})),
    "watch_rule:read": Rule(frozenset({"PLATFORM_OPERATOR"})),
    "watch_rule:update": Rule(frozenset({"PLATFORM_OPERATOR"}), mfa=True),
    "hardware_signal:write": Rule(frozenset({"PLATFORM_OPERATOR"})),
    "inspection:read": Rule(frozenset({"PLATFORM_OPERATOR"})),
    "inspection:write": Rule(frozenset({"PLATFORM_OPERATOR"})),
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
