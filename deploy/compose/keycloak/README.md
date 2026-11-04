# 개발용 Keycloak realm

- realm `marine`, 사용자 9명 (비밀번호 `dev-password`, 개발 전용). ID는 `backend/app/seed.py`의 `idp_subject`와 같다.
- `admin-a`, `admin-b`, `operator`, `operator2`는 첫 로그인 때 OTP 등록을 요구한다 (02번 요구사항 4.1).
- `marine-web`: 웹 화면용 공개 클라이언트(PKCE). `marine-dev-cli`: 개발용 비밀번호 그랜트 (운영 realm에는 만들지 않는다).
- 액세스 토큰에 `aud=marine-api`를 넣는다. 기관·역할은 플랫폼 DB(users 표)가 기준이라 토큰 클레임에 의존하지 않는다.

## 확인 필요
- MFA 여부 판단: API는 `amr`에 `otp`/`mfa`가 있거나 `acr`이 `IDP_MFA_ACR_VALUES`(기본 `mfa,2`)에 있으면 MFA로 본다.
  Keycloak에서 OTP 로그인 시 `acr=2`가 나오도록 단계 인증(acr-to-LoA, 조건부 OTP 흐름)을 설정하는 작업이 남아 있다.
  설정 전까지는 개발 토큰이 MFA로 인정되지 않아 고위험 동작(등록 코드·API 키 발급)이 403 MFA_REQUIRED가 된다.
  통합 시험은 시험용 서명 키로 만든 토큰을 쓴다 (`backend/tests/conftest.py`).

## 개발 토큰 받기 (구성원 계정)
```bash
curl -s -d grant_type=password -d client_id=marine-dev-cli -d username=member-a -d password=dev-password \
  http://localhost:8080/realms/marine/protocol/openid-connect/token
```
