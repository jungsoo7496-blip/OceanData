---
inclusion: always
---

# 기술 스택과 하드웨어 독립 규칙

## 1. 하드웨어 독립 규칙 (최우선)
하드웨어(스토리지 제품, GPU, 서버 대수, 배포 형태)는 아직 정해지지 않았다.
지금 작성하는 코드는 하드웨어가 들어온 뒤 **설정만 바꿔서** 그대로 동작해야 한다.

| 규칙 | 내용 |
|---|---|
| HW-1 스토리지 | S3 API의 공통 기능(PUT/GET/HEAD/DELETE, 멀티파트 업로드, 사전 서명 URL, 목록, 객체 태그)만 쓴다. 특정 제품 전용 API·관리 기능에 의존하지 않는다. 엔드포인트·버킷명·리전·path-style 여부는 설정값이다. |
| HW-2 GPU | GPU를 쓰는 코드는 `InferencePort` 뒤에만 둔다. 기본값 `GPU_ENABLED=false`에서 모든 기능이 CPU로 동작해야 한다(느려도 된다). |
| HW-3 수치 | 용량·쿼터·보존 기간·샘플링 fps·작업 우선순위·임계치·알림 기준은 DB 정책 테이블 또는 환경 설정이다. 코드 상수로 박지 않는다. |
| HW-4 배포 | 운영 배포 형태(VM/Kubernetes)는 미정이다. 산출물은 컨테이너 이미지 + 환경변수 설정까지. 배포 스크립트는 개발용 Docker Compose만 작성한다. |
| HW-5 외부 연계 | 공공 Open API 소스, 알림 채널, 광역 허브, NGSI-LD는 어댑터 + 설정으로 붙인다. |
| HW-6 엣지 | 엣지 수집기는 "동기화 에이전트 + 캡처 플러그인" 구조로 만든다. 업로드 프로토콜은 에이전트와 같다. |
| HW-7 테스트 데이터 | 합성 데이터(ffmpeg testsrc 등)와 공개 샘플만 쓴다. 실제 사람이 찍힌 영상·실제 개인정보는 저장소에 넣지 않는다. |
| HW-8 성능 | 처리량·대역폭 수치를 코드에 가정하지 않는다. 메트릭으로 측정·노출하고 벤치마크 스크립트를 함께 만든다. |

## 2. 스택
| 영역 | 선택 | 메모 |
|---|---|---|
| API 서버·워커 | Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy 2.x, Alembic | API와 워커가 같은 패키지를 공유 |
| 작업 큐 | PostgreSQL 기반 (`QueuePort`로 추상화). 구현은 procrastinate(MIT) PoC 후 결정, 05번 요구사항 1을 못 맞추면 `FOR UPDATE SKIP LOCKED` 자체 구현 | 외부 브로커(Redis 등) 없이 시작 (ADR-0002) |
| DB | PostgreSQL 16 + PostGIS + pgvector | 개발용 커스텀 이미지에 두 확장 포함 |
| 오브젝트 스토리지 | boto3 (S3 API) | 개발: SeaweedFS S3 게이트웨이 컨테이너 / 단위 테스트: moto |
| 인증 | Keycloak (OIDC) | API는 JWT 검증만 한다 |
| API 게이트웨이 | 개발 단계는 FastAPI 미들웨어(호출 제한), 통합 단계에서 Kong 또는 Apache APISIX | 게이트웨이 유무와 무관하게 API가 동작해야 한다 |
| 프론트엔드 | React + TypeScript + Vite, TanStack Query, Ant Design(ko_KR), 웹 업로드 Uppy(S3 멀티파트 모드) | CRUD 화면 프레임워크(Refine)는 PoC 후 결정 |
| 계약 → 코드 | datamodel-code-generator(Pydantic), openapi-typescript(TS), oapi-codegen(Go) | `contracts/`에서 생성한 타입을 손으로 고치지 않는다 |
| 보안·공급망 | Trivy(취약점), Syft(SBOM·라이선스), OWASP ZAP(동적 검사) | 17번 릴리스 게이트 |
| 에이전트 | Go 최신 안정판, 단일 실행파일(Windows·Linux), 로컬 상태 SQLite(순수 Go 드라이버) | |
| 센서 수집 | REST 배치(`POST /v1/observations`) + 서버측 CSV/JSON 파싱 작업 + 에이전트 Sensor_Plugin(MQTT 구독) | 센터에는 MQTT 브로커를 두지 않는다. 현장에서 바깥으로만 연결한다 |
| 계약 검증 | jsonschema(2020-12), openapi-spec-validator, API 계약 시험(Schemathesis 등) | `make contracts-check`를 CI 필수 단계로 둔다 |
| 영상 처리 | FFmpeg / ffprobe를 별도 프로세스로 호출 | 라이브러리로 링크하지 않는다. 하드웨어 가속은 설정으로만 켠다 |
| AI 추론 | ONNX Runtime (CPU / CUDA 실행 공급자) | |
| 보고서 | PDF: HTML→PDF 렌더러(WeasyPrint 등), XLSX: openpyxl, HWPX: 템플릿 XML 치환 | HWPX 구현 방식은 스파이크 후 확정 |
| 관측 | Prometheus 메트릭(`/metrics`), JSON 구조 로그 | Grafana는 compose 선택 서비스 |

## 3. 금지·주의
- **MinIO Community Edition 사용 금지** (유지보수 종료, 저장소 아카이브). 개발용으로도 쓰지 않는다.
- **코드에 포함·링크되는 라이브러리의 라이선스 허용 목록**: MIT, BSD, Apache-2.0, ISC, MPL-2.0, PostgreSQL License, Zlib. EPL/EDL 이중 라이선스는 EDL로 쓴다. 폰트는 OFL-1.1을 허용한다. 그 외(AGPL, GPL, LGPL, BUSL, 비상업 전용 등)는 사전 승인 없이 추가하지 않는다.
  - 별도 서비스·프로세스로 실행하는 인프라(PostgreSQL·PostGIS, FFmpeg 바이너리, Keycloak, SeaweedFS, 개발용 Mosquitto)는 이 목록의 적용 대상이 아니다. 단, FFmpeg 빌드 구성(GPL 구성 포함 여부)은 기록하고 고지에 넣는다.
  - Grafana(AGPL)는 수정하지 않은 운영자 내부용 선택 서비스로만 쓴다. 외부 사용자에게 노출하거나 수정·임베드하지 않는다.
- **재사용 우선**: 직접 만들기 전에 `docs/OSS-CANDIDATES.md`를 본다. "채택"은 바로 쓰고, "PoC"·"조건부"는 `docs/adr/`에 결과 ADR이 있어야 쓰고, "안 씀"은 쓰지 않는다. 목록에 없는 의존성은 허용 목록 확인 후 ADR을 남긴다.
- 릴리스마다 SBOM과 `THIRD_PARTY_NOTICES`를 만든다 (17번).
- **AI 모델 가중치도 라이선스 검토 대상이다.** AGPL 계열 탐지 프레임워크와 비상업 전용 사전학습 모델은 승인 없이 쓰지 않는다. 모델 레지스트리에 라이선스를 기록한다.
- 비밀값(키·비밀번호·토큰)은 코드·저장소에 두지 않는다. `.env.example`만 커밋한다.

## 4. 개발 환경
`deploy/compose/docker-compose.yml` 서비스: postgres(PostGIS+pgvector), seaweedfs(S3), keycloak, api, worker, web, (선택) prometheus, grafana, mailpit

| 명령 | 동작 |
|---|---|
| `make up` / `make down` | 개발 환경 기동·중지 |
| `make migrate` | DB 마이그레이션 적용 |
| `make seed` | 시험용 기관 2곳(A·B)·사용자·실증사업·장비 생성 |
| `make test` | 단위 테스트 (Python·Go·TS) |
| `make test-integration` | compose 기반 통합 테스트 |
| `make test-acceptance` | 수용시험 T01~T20 중 구현된 항목 |
| `make sample-video` | 합성 테스트 영상 생성 |
| `make bench` | 처리량 벤치마크 |
| `make contracts-check` | `tools/validate_contracts.py`: 스키마·예시(통과/실패)·OpenAPI·domain.md 코드값 일치 검사 |

## 5. 결정 대기 항목 (확정되면 이 파일부터 고치고 `docs/adr/`에 ADR을 남긴다)
| 항목 | 현재 기본값 | 바뀌는 조건 |
|---|---|---|
| API 서버 언어 | Python/FastAPI | 발주처가 전자정부 표준프레임워크 적용을 요구하면 API 계층을 Java/Spring 기반으로 재결정 (워커·에이전트·프론트는 유지) |
| 운영 오브젝트 스토리지 | 미정 (S3 API만 사용) | 하드웨어 견적 후 |
| 운영 배포 형태 | 미정 | 하드웨어·운영 인력 확정 후 |
| 알림 채널 | 메일 + 웹훅 | 운영기관 메신저 확정 시 어댑터 추가 |
| 보고서 양식 | 범용 템플릿 엔진 | 관리기관 실제 양식 확보 시 템플릿 추가 |
| 공공 Open API 대상 | 모의 어댑터 | 실제 API 명세·이용조건 확인 후 |
| 센서 기업 연계 방식 | REST 배치 + CSV 파싱, MQTT는 에이전트 플러그인 골격만 | 연계 카드에서 MQTT 브로커·토픽·페이로드 형식 확인 후 플러그인 구현 |
| 기기 토큰 발급 방식 (`POST /v1/auth/token`) | 자체 발급 단기 JWT | 보안 설계에서 Keycloak 클라이언트 자격증명 방식으로 바꿀지 결정 |
