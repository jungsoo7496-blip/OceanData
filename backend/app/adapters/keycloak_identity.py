"""IdentityPort → Keycloak OIDC. API는 JWT 검증만 한다 (tech.md 2장)."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt
from jwt import PyJWKSet

from app.ports.identity import InvalidToken, TokenClaims

log = logging.getLogger(__name__)
JwksProvider = Callable[[], dict[str, Any]]


class KeycloakIdentity:
    """발급자·대상·만료·서명을 검증한다. 기관은 `organization_id` 클레임, 역할은 realm 역할에서 읽는다."""

    def __init__(self, issuer: str, audience: str, *, internal_url: str | None = None,
                 mfa_acr_values: list[str] | None = None, jwks_provider: JwksProvider | None = None,
                 cache_ttl_s: float = 300.0, timeout_s: float = 2.0) -> None:
        self.issuer = issuer.rstrip("/")
        self.base = (internal_url or issuer).rstrip("/")
        self.audience = audience
        self.mfa_acr_values = set(mfa_acr_values or [])
        self.timeout_s = timeout_s
        self._jwks_provider = jwks_provider or self._fetch_jwks
        self._cache_ttl_s = cache_ttl_s
        self._jwks: PyJWKSet | None = None
        self._jwks_at = 0.0
        self._lock = threading.Lock()

    def _fetch_jwks(self) -> dict[str, Any]:
        r = httpx.get(f"{self.base}/protocol/openid-connect/certs", timeout=self.timeout_s)
        r.raise_for_status()
        return dict(r.json())

    def _keyset(self, force: bool = False) -> PyJWKSet:
        with self._lock:
            if force or self._jwks is None or time.monotonic() - self._jwks_at > self._cache_ttl_s:
                self._jwks = PyJWKSet.from_dict(self._jwks_provider())
                self._jwks_at = time.monotonic()
            return self._jwks

    def _key_for(self, kid: str | None) -> Any:
        for force in (False, True):  # 키 교체 대비: 못 찾으면 한 번 새로 받는다
            for k in self._keyset(force).keys:
                if kid is None or k.key_id == kid:
                    return k.key
        raise InvalidToken("unknown_kid")

    def verify_access_token(self, token: str) -> TokenClaims:
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in ("RS256", "ES256", "PS256"):
                raise InvalidToken("bad_alg")
            claims = jwt.decode(
                token, self._key_for(header.get("kid")), algorithms=[header["alg"]],
                audience=self.audience, issuer=self.issuer,
                options={"require": ["exp", "iat", "sub", "iss"]},
            )
        except InvalidToken:
            raise
        except (jwt.PyJWTError, httpx.HTTPError, KeyError, ValueError) as exc:
            raise InvalidToken(type(exc).__name__) from None
        roles = tuple(sorted(set((claims.get("realm_access") or {}).get("roles") or [])))
        amr = claims.get("amr") or []
        mfa = bool(set(amr) & {"otp", "mfa", "hwk", "swk"}) or str(claims.get("acr", "")) in self.mfa_acr_values
        return TokenClaims(subject=str(claims["sub"]), organization_id=claims.get("organization_id"),
                           roles=roles, mfa=mfa, username=claims.get("preferred_username"), raw=claims)

    def check(self) -> bool:
        try:
            r = httpx.get(f"{self.base}/.well-known/openid-configuration", timeout=self.timeout_s)
            return r.status_code == 200
        except httpx.HTTPError:
            return False
