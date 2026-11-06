# 작업: 02 인증·테넌트 격리·감사

- [x] 1. KeycloakIdentity(JWKS 캐시·키 교체) + Auth_Middleware — 요구사항 1.1, 1.2, 1.3 · 시험 `test_user_token_validation_and_audit`
- [x] 2. Permission_Table(기본 거부) — 요구사항 3.1, 3.2 · 시험 `test_permission_default_deny_and_mfa`
- [x] 3. MFA 필요 동작 403 MFA_REQUIRED — 요구사항 4.2 · 시험 `test_mfa_required_for_high_risk_actions`
- [x] 4. Keycloak에서 ORG_ADMIN·PLATFORM_OPERATOR MFA 강제 + acr/amr 클레임 확인 — 요구사항 4.1 · realm 역할 → `mfa-required` 조건부 OTP(브라우저·개발용 비밀번호 그랜트), marine-web 기본 LoA 2 → 관리자 토큰 acr 2·amr otp, 일반 역할 acr 1(OTP 없음) · 시험 `tools/tests/test_keycloak_realm_mfa.py`, `deploy/compose/keycloak/tests/verify-mfa.sh`(Keycloak 26.0.7 일회용 컨테이너·실제 브라우저·API 판정) · ADR-0034
- [x] 5. Tenant_Guard + 교차 기관 404·감사 — 요구사항 2.1, 2.2, 2.5, 2.6 · T01-a·b·c
- [x] 6. PROGRAM_VIEWER 범위 (배정 사업만) — 요구사항 2.3 일부 · T01-f (L1 자산·보고서 범위는 04·10번에서 자산이 생기면 추가)
- [x] 7. 등록 코드·등록·기기 토큰·폐기 — 요구사항 5.1~5.6 · 시험 `test_device_enrollment_token_and_revocation`
- [x] 8. API 키 발급·범위·폐기·만료 — 요구사항 6.1~6.4 · 시험 `test_api_key_lifecycle_and_scope`
- [x] 9. 감사 로그 해시 체인·추가 전용·검증 명령 — 요구사항 7.1~7.4 · 시험 `test_audit_chain_verification_and_tamper_detection`, `make verify-audit`
- [x] 10. 감사 조회 범위 — 요구사항 7.5 · 시험 `test_org_admin_sees_only_own_audit_records`
- [x] 11. 긴급 열람(Break_Glass): 요청·다른 운영자 승인(설정으로 본인 승인)·ORG_ADMIN 통지·원본 접근 감사·만료 즉시 회수·조기 종료 — 요구사항 8.1~8.4 · `/v1/break-glass-grants`(+`/decision`, `/revoke`), download-url 운영자 경로 · 시험 `tests/integration/test_break_glass.py` · ADR-0029, Runbook 05
- [ ] 12. 구성원 관리: 초대·역할 변경(감사)·비활성화 — 요구사항 9.1, 9.2, 3.3 (Keycloak 관리 API 연동 방식 결정 필요)
- [x] 13. T01 수용시험 — T01-a, b, c, d, f, g
- [x] 16. 기기 토큰 범위를 요구사항 5.5로 좁힘 — 요구사항 5.5, 2.2 · Permission_Table `device` 표시(허용 목록: 자기 확인·정책·원격 상태·하트비트·매핑·업로드·관측값·이벤트)와 Tenant_Guard 기기 범위(자기 수집기·매핑된 원천 장비). 보고서 초안·회차 기록 작성·매핑 밖 관측값 열람이 막힘. 시험 `test_device_reach.py`. API 키는 요구사항 6.3·6.4 그대로(scope별 허용이 요구사항에 명시되지 않은 부분은 결정 대기). **남음:** 기기가 다른 기관 장비를 찾을 때 교차 접근 감사 미기록(계약 점검 발견)
- [x] 17. 기기 자격 폐기 시 미사용 유효 등록 코드 만료 및 동시 등록 차단 — 요구사항 5.3, 5.4, 7.1 · 순차·경합 회귀 시험 `test_identity.py`, 410 응답 계약 시험 `test_contract_01_02.py` · ADR-0043
- [x] 18. 인증 실패 감사 경로 제한·호출 제한 키 상한·토큰 교환 IP 제한·JWKS 단일 갱신과 재조회 간격 — 요구사항 1.1, 1.2, 7.1~7.3 및 04번 9.1, 9.2 · ADR-0048 · 시험 `test_auth_failure_audit.py`, `test_ratelimit.py`, `test_jwks_refresh.py` · 요청별 감사 집계 전환은 ADR-0049에서 승인 대기
