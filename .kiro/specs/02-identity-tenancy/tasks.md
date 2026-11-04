# 작업: 02 인증·테넌트 격리·감사

- [x] 1. KeycloakIdentity(JWKS 캐시·키 교체) + Auth_Middleware — 요구사항 1.1, 1.2, 1.3 · 시험 `test_user_token_validation_and_audit`
- [x] 2. Permission_Table(기본 거부) — 요구사항 3.1, 3.2 · 시험 `test_permission_default_deny_and_mfa`
- [x] 3. MFA 필요 동작 403 MFA_REQUIRED — 요구사항 4.2 · 시험 `test_mfa_required_for_high_risk_actions`
- [ ] 4. Keycloak에서 ORG_ADMIN·PLATFORM_OPERATOR MFA 강제 + acr/amr 클레임 확인 — 요구사항 4.1 (realm은 OTP 등록 요구까지만, 단계 인증 흐름 미설정)
- [x] 5. Tenant_Guard + 교차 기관 404·감사 — 요구사항 2.1, 2.2, 2.5, 2.6 · T01-a·b·c
- [x] 6. PROGRAM_VIEWER 범위 (배정 사업만) — 요구사항 2.3 일부 · T01-f (L1 자산·보고서 범위는 04·10번에서 자산이 생기면 추가)
- [x] 7. 등록 코드·등록·기기 토큰·폐기 — 요구사항 5.1~5.6 · 시험 `test_device_enrollment_token_and_revocation`
- [x] 8. API 키 발급·범위·폐기·만료 — 요구사항 6.1~6.4 · 시험 `test_api_key_lifecycle_and_scope`
- [x] 9. 감사 로그 해시 체인·추가 전용·검증 명령 — 요구사항 7.1~7.4 · 시험 `test_audit_chain_verification_and_tamper_detection`, `make verify-audit`
- [x] 10. 감사 조회 범위 — 요구사항 7.5 · 시험 `test_org_admin_sees_only_own_audit_records`
- [ ] 11. 긴급 열람(Break_Glass): 요청·다른 운영자 승인·ORG_ADMIN 통지·만료 회수 — 요구사항 8.1~8.4 (테이블만 있음, 서비스·API 없음 → contracts 먼저)
- [ ] 12. 구성원 관리: 초대·역할 변경(감사)·비활성화 — 요구사항 9.1, 9.2, 3.3 (Keycloak 관리 API 연동 방식 결정 필요)
- [x] 13. T01 수용시험 — T01-a, b, c, d, f, g
