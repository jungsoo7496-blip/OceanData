"""기기·API 키 자격증명과 단기 액세스 토큰 (02번 요구사항 5·6).

장기 비밀값은 해시(SHA-256)로만 저장한다. 비밀값은 256비트 난수라 느린 해시가 필요 없다.
단기 토큰은 자체 발급 HS256 JWT다 (tech.md 결정 대기: Keycloak 클라이언트 자격증명으로 바꿀 수 있다).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

import jwt

ISSUER = "marine-platform"
AUDIENCE = "marine-api"


def new_secret(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def verify_secret(secret: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_secret(secret), stored_hash)


@dataclass(frozen=True)
class MachineClaims:
    kind: str  # DEVICE / API_KEY
    client_id: str
    organization_id: str
    subject_id: str  # device_id 또는 api_key_id
    scopes: frozenset[str]
    expires_at: datetime


class MachineTokenIssuer:
    def __init__(self, signing_key: str, ttl_s: int) -> None:
        if len(signing_key) < 32:
            raise ValueError("DEVICE_TOKEN_SIGNING_KEY는 32자 이상이어야 합니다")
        self._key = signing_key
        self.ttl_s = ttl_s

    def issue(self, *, kind: str, client_id: str, organization_id: str, subject_id: str,
              scopes: frozenset[str], now: datetime) -> tuple[str, int]:
        claims = {
            "iss": ISSUER, "aud": AUDIENCE, "sub": client_id, "kind": kind, "org": organization_id,
            "sid": subject_id, "scope": " ".join(sorted(scopes)), "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=self.ttl_s)).timestamp()), "jti": secrets.token_hex(8),
        }
        return jwt.encode(claims, self._key, algorithm="HS256", headers={"typ": "at+jwt", "kid": "machine"}), \
            self.ttl_s

    def is_machine_token(self, token: str) -> bool:
        try:
            return jwt.get_unverified_header(token).get("kid") == "machine"
        except jwt.PyJWTError:
            return False

    def verify(self, token: str, now: datetime) -> MachineClaims:
        c = jwt.decode(token, self._key, algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER,
                       options={"require": ["exp", "iat", "sub", "iss", "aud"], "verify_exp": False})
        if int(c["exp"]) <= int(now.timestamp()):
            raise jwt.ExpiredSignatureError("expired")
        return MachineClaims(kind=c["kind"], client_id=c["sub"], organization_id=c["org"], subject_id=c["sid"],
                             scopes=frozenset(c["scope"].split()), expires_at=datetime.fromtimestamp(c["exp"]))
