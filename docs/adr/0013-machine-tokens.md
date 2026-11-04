# ADR-0013: 기기·API 키 단기 토큰을 자체 발급 JWT로 시작

- 상태: 채택 (tech.md 결정 대기 항목의 현재 기본값)
- 날짜: 2026-11-04
- 관련: 02번 요구사항 5·6, api-conventions.md 인증

## 결정
- 등록 코드·기기 비밀값·API 키 비밀값은 256비트 난수이며 서버에는 SHA-256 해시만 저장한다 (느린 해시 불필요).
- `POST /v1/auth/token`(client_credentials)은 HS256 JWT를 발급한다. 기본 5분 (`DEVICE_TOKEN_TTL_S`). 서명 키는 `DEVICE_TOKEN_SIGNING_KEY`(32자 이상).
- client_id 접두사: 기기 `dcr_`, API 키는 `key_`(= api_key_id).
- **폐기는 즉시 반영한다.** 토큰 검증 때마다 자격증명 상태·만료를 DB에서 확인한다 (요구사항은 5분 이내 무효였지만 더 엄격하게).
- 사용자 역할·기관은 플랫폼 DB(users 표)가 기준이다. Keycloak 토큰은 신원(sub)·MFA 여부만 제공한다.

## 다른 선택지
Keycloak 클라이언트 자격증명으로 기기마다 클라이언트를 만드는 방식. 기기 수가 많고 등록·폐기를 플랫폼이 관리해야 해서 보류. 보안 설계에서 바꾸기로 하면 `services/tokens.py`와 `services/auth.py._machine`만 교체한다.
