# 설계: 02 인증·테넌트 격리·감사

## 인증 흐름
- 사용자: Keycloak 액세스 토큰(RS256) → `KeycloakIdentity`가 서명·iss·aud·exp 검증 → `users.idp_subject`로 사용자 조회. **기관·역할은 플랫폼 DB가 기준**이고 토큰의 `organization_id` 클레임이 있으면 DB와 같아야 한다.
- MFA: `amr`에 otp/mfa 또는 `acr ∈ IDP_MFA_ACR_VALUES`. Keycloak 단계 인증 설정은 확인 필요 (`deploy/compose/keycloak/README.md`).
- 기기·API 키: `POST /v1/auth/token` → 자체 발급 HS256 JWT (ADR-0013). 매 요청 DB에서 자격증명 상태 확인 → 폐기 즉시 401.

## 구성요소
| 용어 | 구현 |
|---|---|
| Auth_Middleware | `app/api/deps.py:get_principal` + `app/services/auth.py:Authenticator` |
| Permission_Table | `app/domain/permissions.py:PERMISSIONS` (역할·기기 범위·MFA) — 표에 없으면 거부 |
| Tenant_Guard | `app/services/tenant.py:scope_filter/get_scoped` — 역할별 데이터 범위 표(domain.md 2장)를 SQL 조건으로 |
| Device_Credential | `device_credentials` (dcr_ 접두사, SHA-256 해시) |
| Api_Key | `api_keys` (key_ 접두사 = client_id, scope 배열, 만료) |
| Audit_Log | `audit_log` + `app/services/audit.py` — 별도 연결로 기록, advisory lock으로 체인 직렬화, 트리거로 UPDATE/DELETE/TRUNCATE 거부, 앱 계정 권한 SELECT·INSERT만 |

## API (contracts 반영)
- 기존: `/v1/auth/token`, `/v1/agents/enroll`, `/v1/devices/{id}/enrollment-codes`, `/v1/me`
- 추가(x-freeze: G1, x-status: draft): `/v1/api-keys`(GET·POST), `/v1/api-keys/{id}/revoke`, `/v1/devices/{id}/credentials/revoke`, `/v1/audit-logs`

## 오류 처리
- 인증 실패 401 + AUTH_FAILED 감사 / 권한 없음 403 FORBIDDEN·MFA_REQUIRED + PERMISSION_DENIED 감사
- 다른 기관 리소스 404 + CROSS_ORG_ACCESS_ATTEMPT 감사 (존재 여부 숨김)
- 쿼리·헤더의 organization_id, 본문의 계약 밖 필드는 400

## 테스트
`backend/tests/integration/test_identity.py`, `backend/tests/acceptance/test_t01_tenant_isolation.py`, `backend/tests/contract/`
