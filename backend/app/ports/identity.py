"""IdentityPort — 사용자 OIDC 토큰 검증 (Keycloak)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class TokenClaims:
    subject: str
    organization_id: str | None
    roles: tuple[str, ...]
    mfa: bool
    username: str | None = None
    raw: dict = field(default_factory=dict, compare=False, repr=False)


class InvalidToken(Exception):
    """서명·발급자·대상·만료 검증 실패. 사유는 로그용 코드만 담는다."""


class IdentityPort(Protocol):
    def verify_access_token(self, token: str) -> TokenClaims: ...
    def check(self) -> bool: ...
