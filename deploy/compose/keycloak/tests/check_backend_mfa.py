#!/usr/bin/env python3
"""mfa-login.cjs가 받은 실제 Keycloak 토큰을 API의 KeycloakIdentity로 검증해 MFA 판정이 기대와 같은지 본다.

서명·발급자·대상·만료를 실제로 검증한다 (시험 컨테이너의 JWKS). backend 의존성(pyjwt, httpx, pydantic)이 필요하다:

    backend/.venv/bin/python deploy/compose/keycloak/tests/check_backend_mfa.py <evidence.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "backend"))

from app.adapters.keycloak_identity import KeycloakIdentity  # noqa: E402
from app.config import Settings  # noqa: E402
from app.ports.identity import InvalidToken  # noqa: E402


def main(path: str) -> int:
    evidence = json.loads(Path(path).read_text(encoding="utf-8"))
    factory = Settings.model_fields["idp_mfa_acr_values"].default_factory
    default_acr = list(factory()) if factory else []
    if sorted(default_acr) != sorted(evidence["mfa_acr_values"]):
        print(f"주의: 브라우저 시험의 MFA acr 값 {evidence['mfa_acr_values']}이 API 기본값 {default_acr}과 다르다")
    identity = KeycloakIdentity(evidence["issuer"], "marine-api", mfa_acr_values=default_acr)
    failures = 0
    for e in evidence["tokens"]:
        try:
            c = identity.verify_access_token(e["access_token"])
        except InvalidToken as exc:
            print(f"실패 {e['scenario']} {e['user']}: 토큰 검증 실패 ({exc})")
            failures += 1
            continue
        ok = c.mfa == e["api_mfa"]
        failures += 0 if ok else 1
        print(f"{'통과' if ok else '실패'} {e['scenario']:<26} {e['user']:<9} acr={c.raw.get('acr')!s:<2} "
              f"amr={c.raw.get('amr')} → API mfa={c.mfa} (기대 {e['api_mfa']})")
    print(f"API 판정 확인: {len(evidence['tokens']) - failures}/{len(evidence['tokens'])} 일치")
    return 1 if failures or not evidence["tokens"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
