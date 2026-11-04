# 요구사항: 02 인증·테넌트 격리·감사

## 소개
모든 기능의 전제가 되는 인증(OIDC), 기관 단위 테넌트 격리, 역할 기반 권한, 기기·API 키 자격증명, 감사 로그, 운영자 긴급 열람을 만든다.

## 범위 밖
- 공유 등급 규칙 (13번 스펙)
- 화면 (09번 스펙) — 이 스펙은 API와 IdP 설정까지

## 용어집
- **Identity_Service**: Keycloak 연동(IdentityPort 구현)
- **Auth_Middleware**: 토큰을 검증하고 요청 컨텍스트(사용자·기관·역할)를 만드는 계층
- **Tenant_Guard**: 저장소 계층에서 기관 범위를 강제하는 구성요소
- **Permission_Table**: 역할별 허용 동작을 선언한 단일 표
- **Device_Credential**: 에이전트·엣지 기기 자격증명
- **Api_Key**: 기관 서버 연동용 키
- **Audit_Log**: 추가 전용 감사 기록 (해시 체인)
- **Break_Glass**: 운영자의 시간 제한 원본 열람 승인

## 요구사항

### 요구사항 1: 사용자 인증
**사용자 스토리:** 사용자로서, 기관 계정으로 로그인하고 싶다.

#### 수용 기준
1. WHEN 요청에 Bearer 액세스 토큰이 있으면, THE Auth_Middleware SHALL 서명·발급자·대상·만료를 검증하고 사용자·기관·역할을 요청 컨텍스트에 설정한다.
2. IF 토큰이 없거나 검증에 실패하면, THEN THE Auth_Middleware SHALL 401 UNAUTHENTICATED를 반환하고 감사 로그에 남긴다.
3. THE Identity_Service SHALL 사용자 한 명을 기관 하나에만 소속시킨다.

### 요구사항 2: 테넌트 격리
**사용자 스토리:** 제공기업으로서, 다른 기관이 우리 데이터를 볼 수 없다는 확신이 필요하다.

#### 수용 기준
1. THE Tenant_Guard SHALL domain.md 2장의 역할별 데이터 범위 표를 저장소 계층에서 그대로 적용한다. 제공기관 역할은 토큰의 organization_id 범위로 제한하고, 다른 기관 데이터가 필요한 역할(PROGRAM_VIEWER, PLATFORM_OPERATOR, REVIEWER, CONSUMER)은 표에 적힌 범위만 허용하며 그 접근을 감사 로그에 남긴다.
2. WHEN 사용자가 다른 기관의 리소스를 요청하면, THE 시스템 SHALL 404 NOT_FOUND를 반환하고 교차 기관 시도로 감사 로그에 남긴다.
3. WHILE 사용자가 PROGRAM_VIEWER이면, THE Tenant_Guard SHALL 배정된 실증사업의 L1 자산 메타데이터와 제출(SUBMITTED)된 보고서·지표만 허용하고, 어떤 등급이든 원본·프록시·다운로드 URL은 주지 않는다.
4. THE 테스트 SHALL 모든 공개 엔드포인트에 대해 기관 A 토큰으로 기관 B 리소스 접근을 시도하고 모두 차단되는지 확인한다 (T01).
5. THE Tenant_Guard SHALL 목록·검색·자동완성·집계를 권한 범위 필터 뒤에 계산해, 결과 건수로 다른 기관 데이터의 존재가 드러나지 않게 한다.
6. IF 요청 본문·쿼리·헤더에 organization_id가 들어 있으면, THEN THE 시스템 SHALL 그 값으로 범위를 바꾸지 않는다. 계약상 받지 않는 필드이면 400 VALIDATION_ERROR로 거부한다.

### 요구사항 3: 역할 권한
**사용자 스토리:** 운영자로서, 역할마다 할 수 있는 일을 한 곳에서 관리하고 싶다.

#### 수용 기준
1. THE Permission_Table SHALL domain.md 2장의 역할별 허용 동작을 코드 한 곳에 선언한다.
2. IF 요청 동작이 Permission_Table에 없으면, THEN THE 시스템 SHALL 403 FORBIDDEN을 반환한다 (기본 거부).
3. WHEN 역할이 바뀌면, THE 시스템 SHALL 변경 전후 역할과 변경자를 감사 로그에 남긴다.

### 요구사항 4: 다중 인증
**사용자 스토리:** 운영기관으로서, 관리자 계정 탈취를 막고 싶다.

#### 수용 기준
1. THE Identity_Service SHALL ORG_ADMIN과 PLATFORM_OPERATOR에게 다중 인증을 요구하도록 IdP를 설정한다.
2. IF 다중 인증 없이 발급된 토큰으로 고위험 동작(공유 승인, 정책 변경, 긴급 열람, 키 발급)을 요청하면, THEN THE 시스템 SHALL 403 MFA_REQUIRED를 반환한다.

### 요구사항 5: 기기 자격증명
**사용자 스토리:** 제공기업 관리자로서, 현장 PC에 에이전트를 안전하게 등록하고 싶다.

#### 수용 기준
1. WHEN ORG_ADMIN이 장비에 대한 등록 코드를 요청하면, THE 시스템 SHALL 1회용·유효시간 제한(기본 24시간, 설정값) 등록 코드를 발급한다.
2. WHEN 에이전트가 유효한 등록 코드를 제출하면, THE 시스템 SHALL 해당 기관의 수집기 장비와 그 수집기에 매핑된 원천 장비로 범위가 제한된 Device_Credential을 발급하고 서버에는 해시만 저장한다.
3. IF 등록 코드가 만료됐거나 이미 사용됐으면, THEN THE 시스템 SHALL 등록을 거부하고 사유를 반환한다.
4. WHEN ORG_ADMIN이 Device_Credential을 폐기하면, THE 시스템 SHALL 이후 요청을 401로 거부하고, 이미 발급된 액세스 토큰은 설정 시간(기본 5분) 안에 무효로 만든다.
5. THE Device_Credential SHALL 매핑된 원천 장비의 업로드·관측값·이벤트와 자기 수집기의 하트비트·정책 조회만 허용한다.
6. WHEN 기기 또는 API 키 주체가 `POST /v1/auth/token`에 자격증명을 제출하면, THE 시스템 SHALL 발급 범위로 제한된 단기 액세스 토큰(기본 5분, 설정값)을 발급한다. 장기 자격증명은 이 엔드포인트에만 보낸다.

### 요구사항 6: API 키
**사용자 스토리:** 자체 서버가 있는 제공기업으로서, 서버에서 직접 데이터를 보내고 싶다.

#### 수용 기준
1. WHEN ORG_ADMIN이 범위(scope)와 만료일을 지정해 API 키를 요청하면, THE 시스템 SHALL 키를 한 번만 보여주고 해시만 저장한다.
2. WHEN API 키가 폐기되거나 만료되면, THE 시스템 SHALL 즉시 해당 키의 요청을 거부한다.
3. THE Api_Key SHALL 발급 기관 범위와 지정 scope 밖의 동작을 허용하지 않는다.
4. THE Api_Key의 scope SHALL upload:write, observation:write, event:write, asset:read, status:read, webhook:manage 중에서 고른다. `POST /v1/auth/token`에서 scope를 좁혀 요청할 수 있다.

### 요구사항 7: 감사 로그
**사용자 스토리:** 제공기업으로서, 누가 언제 우리 데이터에 접근했는지 확인하고 싶다.

#### 수용 기준
1. THE Audit_Log SHALL security.md에 나열된 이벤트를 시각·행위자·기관·행위·대상·결과·출발지 IP·request_id·사유와 함께 기록한다.
2. THE Audit_Log 테이블 SHALL 애플리케이션 DB 계정에 UPDATE·DELETE 권한을 주지 않는다.
3. THE Audit_Log SHALL 각 행에 직전 행의 해시를 포함한다.
4. WHEN 운영자가 검증 명령을 실행하면, THE 시스템 SHALL 해시 체인이 끊긴 위치를 보고한다.
5. WHEN ORG_ADMIN이 감사 기록을 조회하면, THE 시스템 SHALL 자기 기관 데이터와 관련된 기록만 보여준다.

### 요구사항 8: 긴급 열람
**사용자 스토리:** 운영자로서, 장애·사고 대응 때만 원본에 접근하고 싶다. 평소에는 원본을 열지 않는다는 원칙을 지키기 위해서.

#### 수용 기준
1. WHEN PLATFORM_OPERATOR가 사유·대상·기간(최대 24시간)을 적어 원본 열람을 요청하면, THE 시스템 SHALL 다른 운영자의 승인을 받은 뒤에만 시간 제한 권한을 부여한다 (단일 승인 허용 여부는 설정값).
2. WHEN Break_Glass 권한이 부여되면, THE 시스템 SHALL 대상 기관 ORG_ADMIN에게 통지한다.
3. WHILE Break_Glass 권한이 유효하면, THE 시스템 SHALL 모든 원본 접근을 감사 로그에 남긴다.
4. WHEN 기간이 끝나면, THE 시스템 SHALL 권한을 자동으로 회수한다.

### 요구사항 9: 구성원 관리
**사용자 스토리:** 제공기업 관리자로서, 우리 기관 구성원을 직접 관리하고 싶다.

#### 수용 기준
1. THE 시스템 SHALL ORG_ADMIN이 자기 기관 구성원을 초대·역할 변경·비활성화할 수 있게 한다.
2. WHEN 구성원이 비활성화되면, THE 시스템 SHALL 이후 요청을 거부한다.

## 추적
- 기능 ID: IAM-01, SEC-01, ADM-02(일부) (망 구간 분리는 하드웨어 후 운영 배포 스펙에서)
- 수용시험: T01(T01-a, T01-b, T01-c, T01-d, T01-f, T01-g) — 세부는 docs/ACCEPTANCE.md
- 하드웨어 의존: 없음
