"""인증 주체 (요청 컨텍스트). domain 계층이라 외부 라이브러리를 쓰지 않는다."""

from __future__ import annotations

from dataclasses import dataclass, field

DEVICE_SCOPES = frozenset({"upload:write", "observation:write", "event:write", "status:read",
                           "agent:heartbeat", "agent:policy"})
API_KEY_SCOPES = frozenset({"upload:write", "observation:write", "event:write", "asset:read", "status:read",
                            "webhook:manage"})


@dataclass(frozen=True)
class Principal:
    kind: str  # USER / DEVICE / API_KEY / SERVICE
    organization_id: str
    org_type: str
    roles: frozenset[str] = frozenset()
    mfa: bool = False
    user_id: str | None = None
    device_id: str | None = None
    api_key_id: str | None = None
    client_id: str | None = None
    scopes: frozenset[str] = frozenset()
    assigned_project_ids: frozenset[str] = frozenset()
    mapped_source_ids: frozenset[str] = field(default_factory=frozenset)

    @property
    def principal_id(self) -> str:
        return self.user_id or self.device_id or self.api_key_id or self.client_id or "unknown"

    @property
    def is_machine(self) -> bool:
        return self.kind in ("DEVICE", "API_KEY")

    def has_role(self, *roles: str) -> bool:
        return bool(self.roles & set(roles))
