# 요구사항: 01 개발 기반 (하드웨어 없이 개발 가능한 환경)

## 소개
하드웨어가 확정되기 전에 모든 기능을 개발·시험할 수 있는 기반을 만든다.
저장소 골격, Docker Compose 개발 환경, 설정 로딩, 헬스체크, 구조 로그, DB 마이그레이션, 시험 데이터, 포트 인터페이스 골격, CI를 포함한다.

## 범위 밖
- 운영 배포 스크립트(VM/Kubernetes) — 하드웨어 확정 후
- 각 기능의 실제 구현 — 이후 스펙

## 용어집
- **Dev_Environment**: `deploy/compose`의 개발용 서비스 묶음
- **Config_Loader**: 환경변수에서 설정을 읽어 검증하는 모듈
- **Api_Server**: FastAPI 애플리케이션
- **Worker**: 작업 큐를 처리하는 프로세스
- **Seed_Tool**: 시험용 기관·사용자·사업·장비를 만드는 명령
- **Sample_Generator**: 합성 영상·대용량 시험 파일을 만드는 도구
- **CI_Pipeline**: 린트·타입 검사·테스트·보안 검사·라이선스 검사를 수행하는 자동화

## 요구사항

### 요구사항 1: 개발 환경 기동
**사용자 스토리:** 개발자로서, 명령 하나로 전체 개발 환경을 띄우고 싶다. 하드웨어 없이 바로 개발하기 위해서.

#### 수용 기준
1. WHEN 개발자가 `make up`을 실행하면, THE Dev_Environment SHALL postgres(PostGIS·pgvector 포함), seaweedfs(S3), keycloak, api, worker, web 서비스를 기동한다.
2. WHEN 모든 서비스의 헬스체크가 통과하면, THE Dev_Environment SHALL 성공으로 종료한다.
3. IF 한 서비스라도 제한 시간 안에 헬스체크를 통과하지 못하면, THEN THE Dev_Environment SHALL 실패한 서비스 이름을 출력하고 0이 아닌 종료 코드를 반환한다.
4. THE Dev_Environment SHALL GPU와 외부 인터넷 연결 없이 동작한다 (컨테이너 이미지 준비 이후).
5. WHEN 개발자가 `make up`을 실행하면, THE Dev_Environment SHALL 존별 버킷(raw, proc, evidence, datasets, quarantine)을 생성한다 (이름은 설정값).

### 요구사항 2: 설정
**사용자 스토리:** 운영자로서, 하드웨어가 정해진 뒤 설정만 바꿔 배포하고 싶다. 코드를 고치지 않기 위해서.

#### 수용 기준
1. THE Config_Loader SHALL DB 접속, S3 엔드포인트·버킷·자격증명·path-style 여부·리전, IdP 주소, `GPU_ENABLED`, 사전 서명 URL 유효시간을 환경변수에서 읽는다.
2. IF 필수 설정이 없으면, THEN THE Config_Loader SHALL 기동을 중단하고 누락된 키 이름만 출력한다 (값은 출력하지 않는다).
3. THE 저장소 SHALL `.env.example`만 포함하고 실제 비밀값은 포함하지 않는다.

### 요구사항 3: 헬스체크
**사용자 스토리:** 운영자로서, 서비스와 의존 시스템 상태를 확인하고 싶다. 장애를 빨리 알기 위해서.

#### 수용 기준
1. WHEN `GET /healthz`를 받으면, THE Api_Server SHALL 프로세스가 살아 있으면 200을 반환한다.
2. WHEN `GET /readyz`를 받으면, THE Api_Server SHALL DB·S3·IdP 연결을 확인하고 모두 정상이면 200을 반환한다.
3. IF 의존 시스템 중 하나라도 실패하면, THEN THE Api_Server SHALL 503과 실패한 의존 시스템 목록을 반환한다.

### 요구사항 4: 구조 로그와 요청 추적
**사용자 스토리:** 개발자로서, 요청 하나를 끝까지 추적하고 싶다. 장애 원인을 찾기 위해서.

#### 수용 기준
1. THE Api_Server와 Worker SHALL JSON 구조 로그(timestamp, level, request_id, organization_id, event)를 출력한다.
2. WHEN 요청에 `X-Request-Id`가 없으면, THE Api_Server SHALL 새 ID를 만들어 로그와 응답 헤더에 넣는다.
3. THE Api_Server와 Worker SHALL 토큰·비밀번호·API 키·사전 서명 URL을 로그에 남기지 않는다.
4. WHEN 로그에 URL이 포함되면, THE 로거 SHALL 쿼리 문자열을 제거한다.

### 요구사항 5: DB 마이그레이션
**사용자 스토리:** 개발자로서, 스키마를 안전하게 바꾸고 되돌리고 싶다.

#### 수용 기준
1. WHEN `make migrate`를 실행하면, THE 시스템 SHALL 최신 스키마를 적용한다.
2. THE 모든 마이그레이션 SHALL 되돌리기(down)를 포함한다.
3. THE CI_Pipeline SHALL 빈 DB에서 up → down → up이 성공하는지 검증한다.

### 요구사항 6: 시험 데이터
**사용자 스토리:** 개발자로서, 기관 두 곳 이상의 시험 데이터가 필요하다. 테넌트 격리를 시험하기 위해서.

#### 수용 기준
1. WHEN `make seed`를 실행하면, THE Seed_Tool SHALL 제공기관 A·B, 관리기관, 운영기관, 이용기관과 domain.md 2장의 역할별 시험 계정, 실증사업·실증회차·장비를 만든다.
2. WHEN `make seed`를 여러 번 실행해도, THE Seed_Tool SHALL 중복 데이터를 만들지 않는다.
3. WHEN `make sample-video`를 실행하면, THE Sample_Generator SHALL 길이·해상도·코덱·비트레이트를 지정한 합성 영상을 만들고, 촬영시각 메타데이터가 있는 영상과 없는 영상을 각각 만든다.
4. THE Sample_Generator SHALL 사람 얼굴이 없는 합성 패턴만 사용한다.
5. WHERE 대용량 옵션을 지정하면, THE Sample_Generator SHALL 지정 크기(예: 10GB 이상)의 시험 파일을 만든다.

### 요구사항 7: 포트 인터페이스 골격
**사용자 스토리:** 개발자로서, 하드웨어 의존 부분을 처음부터 인터페이스로 분리하고 싶다. 나중에 어댑터만 바꾸기 위해서.

#### 수용 기준
1. THE backend SHALL structure.md 2장의 포트 인터페이스를 `app/ports/`에 정의한다.
2. THE backend SHALL 개발용 어댑터를 제공한다: StoragePort → S3(SeaweedFS), QueuePort → PostgreSQL, IdentityPort → Keycloak, NotifierPort → 로그·메일, ClockPort → 시스템 시계. 나머지 포트는 이후 스펙에서 구현하며 지금은 미구현 예외를 던지는 골격만 둔다.
3. THE `app/domain/` SHALL 프레임워크·DB·S3·HTTP 라이브러리를 import하지 않는다 (CI에서 검사).

### 요구사항 8: 메트릭
**사용자 스토리:** 운영자로서, 서비스 수치를 수집하고 싶다.

#### 수용 기준
1. WHEN `GET /metrics`를 받으면, THE Api_Server SHALL Prometheus 형식으로 요청 수·지연·오류 수를 노출한다.
2. THE Worker SHALL 작업 유형별 대기 수·처리 시간·실패 수를 노출한다.

### 요구사항 9: CI
**사용자 스토리:** 개발자로서, 품질·보안·라이선스 문제를 병합 전에 잡고 싶다.

#### 수용 기준
1. THE CI_Pipeline SHALL 린트(ruff, golangci-lint, eslint), 타입 검사(mypy 또는 pyright, tsc), 단위 테스트를 실행한다.
2. THE CI_Pipeline SHALL 정적 보안 분석(bandit 또는 semgrep, gosec)과 의존성 취약점 검사를 실행한다.
3. IF 라이선스 허용 목록(tech.md 3장) 밖의 의존성이 추가되면, THEN THE CI_Pipeline SHALL 실패하고 패키지 이름과 라이선스를 출력한다.
4. IF 저장소에 비밀값 패턴(키·토큰)이 커밋되면, THEN THE CI_Pipeline SHALL 실패한다.
5. THE CI_Pipeline SHALL `make contracts-check`(`tools/validate_contracts.py`)와 `tools/check_traceability.py`를 실행하고, 실패하면 병합을 막는다.

### 요구사항 10: 오류 응답 형식
**사용자 스토리:** 외부 개발자로서, 모든 오류를 같은 형식으로 받아 처리하고 싶다.

#### 수용 기준
1. THE Api_Server SHALL 모든 오류를 api-conventions.md의 RFC 9457 형식(`application/problem+json`, `code`, `request_id` 포함)으로 반환한다.
2. IF 처리되지 않은 예외가 생기면, THEN THE Api_Server SHALL 500 INTERNAL로 감싸 반환하고 스택·SQL·내부 경로는 로그에만 남긴다.
3. THE Api_Server SHALL 입력 검증 실패를 400 VALIDATION_ERROR로 반환하고 `errors`에 필드별 JSON Pointer와 사유를 담는다.

### 요구사항 11: 기능 상태 조회
**사용자 스토리:** 사용자와 외부 개발자로서, 지금 어떤 기능이 켜져 있는지(GPU 연결 여부 등) 알고 싶다.

#### 수용 기준
1. WHEN 인증된 사용자가 `GET /v1/system/capabilities`를 요청하면, THE Api_Server SHALL API 버전, 지원 스키마 버전, GPU 사용 여부, 켜진 처리 단계, 보고서 출력 형식, 켜진 외부 소스 어댑터 목록을 반환한다.
2. THE Api_Server SHALL 이 응답에 엔드포인트 주소·버킷 이름·자격증명 같은 설정값을 넣지 않는다.

## 추적
- 기능 ID: (기반), API-02(오류 형식·기능 상태 일부)
- 수용시험: T03(T03-a), T05(T05-b), T13(T13-b) — 세부는 docs/ACCEPTANCE.md
- 하드웨어 의존: 없음
