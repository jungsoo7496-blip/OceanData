# ADR-0034: Keycloak MFA 강제 — mfa-required 역할 조건부 OTP + LoA 2(acr 2) 기본 요청 + amr 매퍼

- 상태: accepted
- 날짜: 2026-11-05
- 관련: 02번 요구사항 4.1·4.2, 02번 작업 4, `deploy/compose/keycloak/realm-marine.json`, `backend/app/adapters/keycloak_identity.py`

## 맥락
요구사항 4.1은 ORG_ADMIN·PLATFORM_OPERATOR에게 IdP가 다중 인증을 요구하는 것이다. 기존 개발 realm은 네 계정에 OTP 등록
필수 동작(CONFIGURE_TOTP)만 걸어 두었고, 토큰에는 MFA 표시가 없었다(acr 항상 1, amr 없음). API는 이미 `amr`에 otp/mfa/hwk/swk가
있거나 `acr`이 `IDP_MFA_ACR_VALUES`(기본 `mfa,2`)에 있으면 MFA로 보고, 없으면 고위험 동작을 403 MFA_REQUIRED로 막는다(요구사항 4.2).
역할은 플랫폼 DB(users.roles)가 기준이라 Keycloak은 누가 관리자인지 몰랐다.

Keycloak 26.0.7에서 직접 확인한 제약:
- `Condition - Level of Authentication`은 클라이언트가 그 수준 이상을 요청할 때만 맞는다. 요청이 없으면 LoA 2 흐름은 돌지 않는다.
  사용자 역할로 요청 수준을 정할 수는 없다.
- 브라우저 흐름에 같은 LoA 수준 조건이 두 개 있으면 로그인 중 `IllegalStateException: Duplicate key 2`로 실패한다.
- 클라이언트 `minimum.acr.value`는 브라우저 흐름의 요청 수준을 올리지 않았다(관리자가 OTP 없이 acr 1 토큰을 받음).
- 클라이언트가 LoA 2를 기본 요청하면, LoA 1 세션의 일반 사용자는 다시 인가를 받을 때 단계 인증으로 갈 곳이 없어
  "Invalid username or password" 오류가 났다(SSO가 끊김).
- AMR 매퍼는 실행 설정 `default.reference.value`가 있는 인증기만 amr에 넣는다. `default.reference.maxAge`가 없을 때는 같은 계정·흐름에서도
  amr이 빈 배열로 나오는 로그인이 섞였고, maxAge 36000을 주자 매번 들어갔다.
- OTP 등록(CONFIGURE_TOTP)으로 끝난 로그인은 amr에 otp를 남기지 않는다.

## 결정
1. **표지 역할**: realm 역할 `ORG_ADMIN`·`PLATFORM_OPERATOR`를 만들고 둘 다 복합 역할로 `mfa-required`를 포함한다. 흐름 조건은
   `mfa-required` 하나만 본다(역할 조건은 OR이 안 된다). 개발 계정 역할은 seed.py와 같게 준다. 권한 판단은 계속 플랫폼 DB 기준이고
   realm 역할은 MFA 강제에만 쓴다. 계정별 CONFIGURE_TOTP는 지운다(역할이 강제한다).
2. **브라우저 흐름 `browser-mfa`** (realm 기본 브라우저 흐름):
   - 쿠키(ALT) | 양식(ALT) → LoA 1 조건 + 사용자명·비밀번호 / `mfa-required` 조건 → [LoA 2 조건 + OTP] 또는(앞 대안이 비면) OTP /
     `mfa-required` 부정 조건 → 통과(allow-access).
   - `mfa-required` 사용자는 클라이언트가 무엇을 요청하든 OTP를 거친다(`acr_values=1`로 낮춰도). OTP 미등록이면 등록 화면이 나온다.
   - 마지막 부정 조건 통과는 일반 사용자가 LoA 1 세션으로 LoA 2 요청을 받았을 때 현재 수준(acr 1)으로 마치게 하려는 것이다.
     로그인 전에는 LoA 1(비밀번호)이 먼저라 이 단계로 자격 증명 없이 들어올 수 없다(acr_values=0도 로그인 화면으로 감, 시험함).
3. **marine-web 기본 ACR 2** (`default.acr.values=2`): 관리자 토큰은 acr `2`(첫 로그인의 OTP 등록 포함), 일반 사용자는 acr `1`.
   `2`는 API 기본 `IDP_MFA_ACR_VALUES`에 있으므로 API 변경이 필요 없다. acr-to-LoA 이름 매핑은 두지 않는다(숫자 그대로).
4. **amr 매퍼**: marine-web·marine-dev-cli에 AMR 매퍼, 비밀번호 실행에 `pwd`, OTP 실행에 `otp`(maxAge 36000). acr을 요청하지 않는
   클라이언트(계정 콘솔 등)나 `acr_values=1` 요청에서도 OTP를 거친 토큰은 amr `["pwd","otp"]`로 MFA가 된다.
5. **개발용 비밀번호 그랜트 `direct-grant-mfa`**: `mfa-required`면 OTP 등록 시 `totp` 파라미터 필수, 미등록이면 Deny access(401).
   기본 흐름처럼 OTP 실행을 그냥 REQUIRED로 두면 미등록 사용자에서 서버 오류(500)가 났다.
6. LoA 최대 유지 시간은 1·2 모두 36000초(세션 최대와 같음). 같은 SSO 세션에서는 OTP를 다시 묻지 않는다.

버린 대안:
- 역할 조건부 OTP만(LoA 없음): 단순하지만 acr이 항상 1이고 OTP 등록으로 끝난 첫 로그인 토큰이 MFA로 인정되지 않는다.
- LoA 단계 인증만(클라이언트 요청): 관리자가 요청을 낮추면(`acr_values=1`) OTP 없이 토큰을 받는다. 요구사항 4.1을 못 지킨다.
- `minimum.acr.value`: 26.0.7에서 효과가 없었다.
- 역할별 LoA 2 하위 흐름 두 개: Duplicate key 오류.
- 계정별 CONFIGURE_TOTP 유지: 새 관리자에게 빠뜨리면 강제가 사라진다.

## 결과
- 좋아지는 점: 관리자 계정은 IdP에서 OTP 없이 로그인할 수 없고, 웹에서 받은 토큰은 acr 2 + amr otp로 API의 MFA 판정을 통과한다.
  일반 역할은 OTP 없이 그대로다. `tools/tests/test_keycloak_realm_mfa.py`(CI, 구조 검사)와
  `deploy/compose/keycloak/tests/verify-mfa.sh`(일회용 Keycloak 26.0.7 + 브라우저 + API의 KeycloakIdentity로 토큰 판정)로 확인한다.
- 감수할 점:
  - Keycloak realm 역할과 플랫폼 DB 역할을 맞춰야 한다. 어긋나면(IdP에 역할 없음) IdP는 OTP를 묻지 않고, API는 고위험 동작만
    403 MFA_REQUIRED로 막는다. 구성원 관리(02번 작업 12)에서 역할을 바꿀 때 Keycloak 역할도 같이 바꿔야 한다.
  - 첫 로그인의 OTP 등록은 비밀번호만 아는 사람이 자기 OTP를 등록할 수 있다(최초 등록 신뢰). 운영 realm은 관리자 등록 절차를 따로 정한다.
  - `acr_values=1`로 요청하고 그 로그인에서 OTP를 처음 등록한 경우 토큰은 acr 1·amr pwd라 MFA로 인정되지 않는다(API가 막고, 다음 로그인부터 amr otp).
  - 비밀번호 그랜트에서 OTP 미등록 관리자는 401과 HTML 오류 본문을 받는다(Deny access 인증기 동작). 개발용 클라이언트에만 해당한다.
  - 흐름이 기본 흐름보다 복잡하다. 관리 콘솔에서 고칠 때 LoA 수준당 조건 하나, OTP는 `mfa-required` 조건 아래라는 규칙을 시험이 지킨다.
- 되돌리는 방법: realm JSON에서 `browserFlow`·`directGrantFlow`를 지우면 Keycloak 기본 흐름으로 돌아간다. 역할·매퍼·기본 ACR은 남겨도
  해가 없다. 이미 떠 있는 개발 Keycloak에는 컨테이너를 다시 만들어(`--force-recreate keycloak`) realm을 새로 가져와야 반영된다.
