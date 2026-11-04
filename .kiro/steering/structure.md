---
inclusion: always
---

# 저장소 구조와 작업 규칙

## 1. 디렉터리
```
/
├─ AGENTS.md                 # AI 작업 규칙 (Kiro가 항상 읽음)
├─ contracts/                # 단일 기준: API·데이터 계약 (코드보다 먼저 고친다)
│  ├─ openapi.yaml           # OpenAPI 3.1 — 외부 연계 API v1
│  ├─ schemas/               # JSON Schema 2020-12 (공통 메타데이터 v1.0 등)
│  ├─ codes/                 # 코드표·단위 사전 (JSON)
│  └─ examples/              # 통과해야 하는 예시, invalid/는 실패해야 하는 예시
├─ backend/                  # Python: API + 워커 (하나의 패키지)
│  ├─ app/
│  │  ├─ api/                # FastAPI 라우터 (HTTP 계층만)
│  │  ├─ domain/             # 엔터티·규칙·상태 전이 (외부 의존 없음)
│  │  ├─ services/           # 유스케이스 (도메인 + 포트 조합)
│  │  ├─ ports/              # 인터페이스
│  │  ├─ adapters/           # 포트 구현 (s3, pg_queue, onnx, keycloak, email, webhook, 외부 소스 등)
│  │  ├─ workers/            # 작업 유형별 핸들러
│  │  └─ db/                 # SQLAlchemy 모델, Alembic 마이그레이션
│  └─ tests/                 # unit / integration / contract
├─ agent/                    # Go: 동기화 에이전트 (캡처·센서 플러그인 인터페이스 포함)
├─ web/                      # React: 워크스페이스·관리기관 보드·운영 콘솔·카탈로그
├─ deploy/compose/           # 개발용 Docker Compose
├─ tests/acceptance/         # 수용시험 T01~T20 (세부 케이스는 docs/ACCEPTANCE.md)
├─ tools/                    # 계약 검증, 합성 데이터 생성, 벤치마크
└─ docs/                     # 기준 문서, 로드맵, 수용시험 카탈로그, 결정 기록(adr/)
```

## 2. 포트·어댑터 규칙
- `domain/`은 프레임워크·DB·S3·HTTP를 import하지 않는다.
- 외부 시스템은 반드시 `ports/` 인터페이스로만 접근한다. 하드웨어가 바뀌면 `adapters/`만 교체한다.
- 포트 목록: `StoragePort`, `QueuePort`, `InferencePort`, `IdentityPort`, `NotifierPort`, `ExternalSourcePort`, `ReportRendererPort`, `ClockPort`(테스트용 시간 고정)
- 에이전트 확장 인터페이스: `Capture_Plugin`(영상 캡처), `Sensor_Plugin`(MQTT 구독 등). 둘 다 결과를 에이전트 대기열에 넣고 같은 API로 보낸다.
- 공유 가능 조건(domain.md 4장)과 상태 전이(domain.md 5장)는 `domain/`에 한 곳만 구현한다. API·워커·화면은 그것을 호출한다. 규칙을 복제하지 않는다.
- API·스키마·코드표는 `contracts/`가 기준이다. Python·TS·Go 타입은 여기서 생성하거나 이것으로 검증한다.

## 3. 이름 규칙
- DB 테이블·컬럼, API JSON 필드: `snake_case`
- ID: 접두사 + ULID(대문자 Crockford Base32 26자) — `org_` 기관, `prj_` 실증사업, `ses_` 실증회차, `dev_` 장비, `ast_` 자산, `upl_` 업로드, `evt_` 실증 이벤트, `shr_` 공유 신청, `dst_` 데이터셋, `dsv_` 데이터셋 버전, `acr_` 이용 신청, `grt_` 이용 권한, `dlv_` 배포, `job_` 작업, `rec_` 회차 기록, `rpt_` 보고서, `whk_` 웹훅 구독, `key_` API 키, `usr_` 사용자, `aud_` 감사 로그, `agr_` 기관 약정, `icd_` 연계 카드, `brk_` 긴급 열람 승인
- 새 엔터티는 세 글자 접두사 + ULID로 만들고 이 목록과 `contracts/schemas/common.schema.json`에 함께 추가한다.
- `X-Request-Id`는 접두사 없는 ULID다.
- 시각 필드는 `_at` 접미사, UTC 저장
- 마이그레이션은 되돌리기(down)를 포함한다

## 4. 언어
- 문서, 스펙, 주석, 커밋 본문, 화면 문구, 오류 메시지(title·detail): **한국어**
- 코드 식별자, 커밋 접두사(feat/fix/docs/test/chore/refactor), 로그 키, 오류 코드: 영어
- 사용자 화면의 시각은 KST로 표시한다

## 5. Kiro 스펙 작성 규칙
- 스펙 하나는 기능 영역 하나다. `.kiro/specs/NN-이름/`, 번호는 개발 순서다.
- requirements.md: 사용자 스토리 + EARS 수용 기준. 컴포넌트 이름은 해당 문서 용어집의 영문 이름을 쓴다. 모든 기준은 테스트할 수 있어야 한다.
- design.md와 tasks.md는 Kiro가 requirements.md를 읽고 만든다. 미리 써 둔 틀을 채우지 않는다.
- design.md: 데이터 모델 변경, API 변경(contracts/ 차이), 사용하는 포트, 상태 전이, 오류 처리, 테스트 전략만 적는다. 다이어그램은 꼭 필요할 때만.
- tasks.md: 태스크 하나는 반나절~하루 분량. 각 태스크에는 **그 태스크가 실제로 구현하는** 요구사항 번호를 단다 (번호 순서대로 짝짓지 않는다). 테스트 작성을 포함한다.
- 계약 우선: API·스키마를 바꾸는 태스크는 `contracts/` 수정 → `make contracts-check` 통과 → 구현 → 계약 시험 순서로 한다.
- 앞선 스펙에서 만든 것을 다시 만들지 않는다. 바꿔야 하면 변경 범위를 명시한다.
- 확인되지 않은 외부 사실(외부 API 명세, 보고서 양식 등)은 지어내지 않는다. "확인 필요"로 표시하고 어댑터 인터페이스와 모의 구현만 만든다.
- 새로 정한 설계 결정은 `docs/adr/`에 ADR로 남긴다.

## 6. 테스트 원칙
- 도메인 규칙(공유 가능 조건, 상태 전이, 멱등성)은 단위 테스트 + 속성 기반 테스트(Hypothesis 등)
- 모든 API에 교차 기관 접근 차단 테스트를 붙인다 (T01). 목록·검색은 결과 건수로도 다른 기관 데이터가 드러나지 않는지 확인한다.
- `contracts/examples/`의 예시는 모두 통과하고 `contracts/examples/invalid/`의 예시는 모두 실패해야 한다 (`make contracts-check`).
- API 응답은 `contracts/openapi.yaml` 기준 계약 시험으로 검증한다.
- 수용시험(T01~T20)은 `tests/acceptance/` 아래 같은 번호로 두고, 세부 케이스는 `docs/ACCEPTANCE.md`의 ID(T01-a 등)를 쓴다.
- 외부 의존은 compose 컨테이너 또는 테스트 대역(moto 등)으로 대체한다
