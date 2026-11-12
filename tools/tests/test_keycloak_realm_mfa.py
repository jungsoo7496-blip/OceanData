"""02번 요구사항 4.1 (ADR-0034): 개발 realm(realm-marine.json)의 MFA 강제 구성이 깨지지 않았는지 정적으로 본다.

실제 로그인 동작은 deploy/compose/keycloak/tests/verify-mfa.sh(일회용 Keycloak + 브라우저)로 확인한다.
여기서는 Docker 없이 CI에서 돌 수 있는 구조 검사만 한다:
- 브라우저·비밀번호 그랜트 흐름 바인딩, 참조된 흐름·설정이 모두 있는지
- LoA 조건 수준이 겹치지 않는지 (같은 수준 두 개면 Keycloak이 로그인 중 Duplicate key 오류를 낸다)
- ORG_ADMIN·PLATFORM_OPERATOR만 mfa-required를 포함하고, 시드 사용자 역할과 realm 역할이 같은지
- OTP는 mfa-required 조건 아래에만, 통과(allow-access)는 mfa-required 부정 조건 아래에만 있는지
- marine-web 기본 ACR(2)이 API 기본 IDP_MFA_ACR_VALUES에 들어 있고, LoA 2 흐름에 OTP가 있는지
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REALM = ROOT / "deploy/compose/keycloak/realm-marine.json"
SEED = ROOT / "backend/app/seed.py"
CONFIG = ROOT / "backend/app/config.py"
PRIVILEGED = {"ORG_ADMIN", "PLATFORM_OPERATOR"}
MARKER = "mfa-required"
OTP_AUTHENTICATORS = {"auth-otp-form", "direct-grant-validate-otp"}


class RealmMfaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.realm = json.loads(REALM.read_text(encoding="utf-8"))
        cls.flows = {f["alias"]: f for f in cls.realm["authenticationFlows"]}
        cls.configs = {c["alias"]: c["config"] for c in cls.realm["authenticatorConfig"]}

    def walk(self, alias, conditions=()):
        """흐름을 따라 내려가며 (실행, 그 실행에 걸린 조건 설정들)을 낸다."""
        flow = self.flows[alias]
        own = tuple(self.configs.get(e.get("authenticatorConfig"), {}) | {"_by": e["authenticator"]}
                    for e in flow["authenticationExecutions"]
                    if not e["authenticatorFlow"] and e["authenticator"].startswith("conditional-"))
        for e in flow["authenticationExecutions"]:
            if e["authenticatorFlow"]:
                yield from self.walk(e["flowAlias"], conditions + own)
            elif not e["authenticator"].startswith("conditional-"):
                yield e, conditions + own

    def test_flow_bindings_and_references(self):
        self.assertEqual(self.realm["browserFlow"], "browser-mfa")
        self.assertEqual(self.realm["directGrantFlow"], "direct-grant-mfa")
        for alias in ("browser-mfa", "direct-grant-mfa"):
            self.assertTrue(self.flows[alias]["topLevel"])
        used = []
        for flow in self.flows.values():
            for e in flow["authenticationExecutions"]:
                if e["authenticatorFlow"]:
                    self.assertIn(e["flowAlias"], self.flows)
                if "authenticatorConfig" in e:
                    self.assertIn(e["authenticatorConfig"], self.configs)
                    used.append(e["authenticatorConfig"])
        self.assertEqual(len(used), len(set(used)), "설정은 실행 하나에만 붙인다")

    def test_loa_levels_are_unique(self):
        levels = [cfg["loa-condition-level"] for e, conds in self.walk("browser-mfa") for cfg in conds
                  if cfg["_by"] == "conditional-level-of-authentication"]
        loa_configs = [e for f in self.flows.values() for e in f["authenticationExecutions"]
                       if e.get("authenticator") == "conditional-level-of-authentication"]
        self.assertEqual(sorted({self.configs[e["authenticatorConfig"]]["loa-condition-level"] for e in loa_configs}),
                         ["1", "2"])
        self.assertEqual(len(loa_configs), 2, "LoA 수준마다 조건은 하나만 (Duplicate key 오류 방지)")
        self.assertTrue(levels)

    def test_only_privileged_roles_carry_marker_and_seed_matches(self):
        roles = {r["name"]: r for r in self.realm["roles"]["realm"]}
        carriers = {n for n, r in roles.items() if MARKER in (r.get("composites") or {}).get("realm", [])}
        self.assertEqual(carriers, PRIVILEGED)
        user_row = re.compile(r'\(fid\("usr", \d+\), \w+, "([\w-]+)", \[([^\]]*)\]')
        seed = {m.group(1): set(re.findall(r'"([A-Z_]+)"', m.group(2)))
                for m in user_row.finditer(SEED.read_text(encoding="utf-8"))}
        self.assertTrue(seed, "seed.py 사용자 목록을 읽지 못했다")
        users = {u["username"]: set(u.get("realmRoles", [])) for u in self.realm["users"]}
        self.assertEqual(set(seed), set(users))
        for name, db_roles in seed.items():
            self.assertEqual(users[name] & PRIVILEGED, db_roles & PRIVILEGED, f"{name}: realm 역할이 시드와 다르다")
            self.assertNotIn(MARKER, users[name], "표지 역할은 직접 주지 않고 ORG_ADMIN·PLATFORM_OPERATOR로만 받는다")

    def test_otp_only_under_marker_and_allow_only_under_negated_marker(self):
        def role_conds(conds):
            return {(c["condUserRole"], c.get("negate") == "true")
                    for c in conds if c["_by"] == "conditional-user-role"}

        for top in ("browser-mfa", "direct-grant-mfa"):
            for e, conds in self.walk(top):
                if e["authenticator"] in OTP_AUTHENTICATORS:
                    self.assertIn((MARKER, False), role_conds(conds), f"{top}: OTP가 mfa-required 조건 밖에 있다")
                    self.assertEqual(e["requirement"], "REQUIRED")
                if e["authenticator"] == "allow-access-authenticator":
                    self.assertEqual(role_conds(conds), {(MARKER, True)}, "통과는 mfa-required가 아닌 사용자만")
        privileged = [e for e, conds in self.walk("browser-mfa")
                      if e["authenticator"] == "auth-otp-form"]
        self.assertEqual(len(privileged), 2, "LoA 2 요청 때와 요청이 없을 때 모두 OTP")

    def test_default_acr_is_mfa_for_api_and_level_two_has_otp(self):
        web = next(c for c in self.realm["clients"] if c["clientId"] == "marine-web")
        default_acr = web["attributes"]["default.acr.values"]
        m = re.search(r"idp_mfa_acr_values: CsvList = Field\(default_factory=lambda: \[([^\]]*)\]\)",
                      CONFIG.read_text(encoding="utf-8"))
        self.assertIsNotNone(m, "config.py의 IDP_MFA_ACR_VALUES 기본값을 찾지 못했다")
        self.assertIn(default_acr, re.findall(r'"([^"]+)"', m.group(1)))
        by_level = {}
        for e, conds in self.walk("browser-mfa"):
            for c in conds:
                if c["_by"] == "conditional-level-of-authentication":
                    by_level.setdefault(c["loa-condition-level"], set()).add(e["authenticator"])
        self.assertEqual(by_level[default_acr], {"auth-otp-form"})
        self.assertNotIn("auth-otp-form", by_level["1"])

    def test_amr_mapper_and_references(self):
        for client in self.realm["clients"]:
            mappers = {m["protocolMapper"] for m in client.get("protocolMappers", [])}
            self.assertIn("oidc-amr-mapper", mappers, client["clientId"])
        for flow in self.flows.values():
            for e in flow["authenticationExecutions"]:
                if e.get("authenticator") in OTP_AUTHENTICATORS:
                    # API는 amr에 otp가 있으면 MFA로 본다 (keycloak_identity.py)
                    self.assertEqual(self.configs[e["authenticatorConfig"]]["default.reference.value"], "otp")


if __name__ == "__main__":
    unittest.main()
