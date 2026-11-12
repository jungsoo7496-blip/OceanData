# 개발용 Keycloak realm

- realm `marine`, 사용자 9명 (비밀번호 `dev-password`, 개발 전용). ID는 `backend/app/seed.py`의 `idp_subject`와 같다.
- `marine-web`: 웹 화면용 공개 클라이언트(PKCE). `marine-dev-cli`: 개발용 비밀번호 그랜트 (운영 realm에는 만들지 않는다).
- 액세스 토큰에 `aud=marine-api`를 넣는다. 기관·역할은 플랫폼 DB(users 표)가 기준이라 API는 토큰의 역할 클레임을 쓰지 않는다.
- realm 파일을 바꾼 뒤에는 keycloak 컨테이너를 다시 만들어 새로 가져온다:
  `docker compose --env-file .env -f deploy/compose/docker-compose.yml up -d --force-recreate keycloak`
  (컨테이너를 다시 만들면 등록한 OTP도 지워진다.)

## 다중 인증 (02번 요구사항 4.1, ADR-0034)
- realm 역할 `ORG_ADMIN`·`PLATFORM_OPERATOR`가 표지 역할 `mfa-required`를 포함하고, 로그인 흐름은 `mfa-required`면 OTP를 강제한다.
  realm 역할은 MFA 강제에만 쓰고, 플랫폼 DB 역할과 같게 맞춰 둔다 (구성원 관리에서 역할을 바꾸면 Keycloak 역할도 바꿔야 한다).
- 개발 계정 영향:
  - `admin-a`, `admin-b`(ORG_ADMIN), `operator`, `operator2`(PLATFORM_OPERATOR): 첫 브라우저 로그인에서 비밀번호 다음 OTP 등록 화면이
    나온다 (인증 앱으로 QR 또는 "Unable to scan?"의 키). 이후 로그인마다 OTP를 입력한다. 계정별 필수 동작이 아니라 역할로 강제한다.
  - `member-a`, `member-b`, `viewer`, `reviewer`, `consumer`: 비밀번호만. OTP를 묻지 않는다.
- 토큰 (Keycloak 26.0.7에서 확인, `tests/verify-mfa.sh`):

  | 경우 | acr | amr | API MFA 판정 |
  |---|---|---|---|
  | 일반 역할 로그인·SSO·비밀번호 그랜트 | `1` | `["pwd"]` | 아님 |
  | 관리자 첫 로그인 (OTP 등록) | `2` | `["pwd"]` | MFA (acr) |
  | 관리자 로그인·SSO·토큰 갱신 | `2` | `["pwd","otp"]` | MFA |
  | 관리자, 클라이언트가 `acr_values=1` 요청 | `1` | `["pwd","otp"]` (등록하던 로그인은 `["pwd"]`) | MFA (amr) / 등록 로그인은 아님 |
  | 관리자 비밀번호 그랜트 + `totp` | `1` | `["pwd","otp"]` | MFA (amr) |

  `marine-web`은 기본으로 LoA 2(`acr=2`)를 요청한다 (`default.acr.values`). API는 `amr`에 `otp`/`mfa`가 있거나 `acr`이
  `IDP_MFA_ACR_VALUES`(기본 `mfa,2`)에 있으면 MFA로 본다. 일반 역할은 LoA 2를 요청해도 OTP 없이 acr 1로 끝난다.
- 확인: `deploy/compose/keycloak/tests/verify-mfa.sh` — 일회용 컨테이너(기본 127.0.0.1:18080)에 이 realm을 올리고 실제 브라우저로
  위 표를 확인한 뒤 API의 `KeycloakIdentity`로 토큰을 검증한다 (`backend/.venv`가 있을 때). 구조 검사는 `tools/tests/test_keycloak_realm_mfa.py`.
- OTP를 잃어버린 개발 계정: 관리 콘솔(realm marine → Users → 계정 → Credentials)에서 OTP를 지우면 다음 로그인에 다시 등록한다.

## 개발 토큰 받기
```bash
# 구성원 계정 (MFA 아님)
curl -s -d grant_type=password -d client_id=marine-dev-cli -d username=member-a -d password=dev-password \
  http://localhost:8080/realms/marine/protocol/openid-connect/token
# 관리자 계정 (브라우저에서 OTP를 먼저 등록한 뒤, 인증 앱의 6자리 코드를 totp로 보낸다 → amr에 otp, MFA)
curl -s -d grant_type=password -d client_id=marine-dev-cli -d username=admin-a -d password=dev-password -d totp=123456 \
  http://localhost:8080/realms/marine/protocol/openid-connect/token
```
OTP 등록 전 관리자 계정의 비밀번호 그랜트는 401(Deny access, 본문은 HTML 오류 화면)이고, `totp`가 틀리거나 없으면 401 `invalid_grant`다.
통합 시험은 여전히 시험용 서명 키로 만든 토큰을 쓴다 (`backend/tests/conftest.py`).
