# 개발 로드맵 v0.2 — 하드웨어 없이 먼저 만들기

## 1. 스펙 진행 순서 (폴더 번호 = 진행 순서)

| 번호 | 스펙 | 핵심 산출물 | 선행 | 하드웨어 없이 | 수용시험 | 규모 | 재사용 (docs/OSS-CANDIDATES.md) |
|---|---|---|---|---|---|---|---|
| 01 | dev-foundation | Compose 개발 환경, 설정, 포트 골격, CI, 오류 형식, 기능 상태 API | — | 가능 | T03, T05, T13 | 중 | FastAPI, SQLAlchemy, Trivy, Syft |
| 02 | identity-tenancy | OIDC, 테넌트 격리, 기기·API 키, 토큰 교환, 감사 로그 | 01 | 가능 | T01 | 중 | Keycloak |
| 03 | metadata-standard | 데이터 모델, 스키마 서비스, 코드표, 사전 검증, 연계 카드·약정 기록 | 02 | 가능 | T05, T06, T13 | 중 | jsonschema, datamodel-code-generator |
| 04 | ingestion-api | 업로드·재개·쿼터 예약·무결성·격리·웹훅 | 03 | 가능 | T01, T02, T03, T04, T05, T12, T13, T15 | 대 | boto3, Schemathesis |
| 05 | processing-pipeline | 작업 큐, 메타 추출, 프록시, 무결성 로그 | 04 | 가능 (CPU) | T04, T06, T12, T16 | 중 | procrastinate(PoC), FFmpeg, ONNX Runtime |
| 06 | sync-agent | Go 에이전트 | 04 | 가능 | T02, T03, T05, T07 | 대 | fsnotify, modernc sqlite, kardianos/service, oapi-codegen |
| 07 | sensor-event-ingestion | 관측값 배치·CSV 파싱·MQTT 플러그인·실증 이벤트 | 03, 04, 05 (MQTT는 06) | 가능 | T05, T06, T07, T13 | 중 | Frictionless(PoC), Mosquitto(시험용), paho MQTT |
| 08 | storage-lifecycle | 쿼터, 보존·dry-run, 삭제 증명, 승격 백업, Hot/Warm 계층 | 04 | 가능 | T03, T06, T10, T12, T15 | 중 | pgBackRest·rclone(PoC) |
| 09 | workspace-portal | 워크스페이스, 온보딩(연계 카드), 관리기관 보드, 운영 콘솔 | 02~08 | 가능 | T01, T02, T07 | 대 | Refine+Ant Design(PoC), Uppy, openapi-typescript |
| 10 | evidence-reports | 회차 기록, 증빙, 지표, 보고서 (PDF·XLSX·HWPX) | 07, 09 | 가능 (범용 템플릿) | T06, T11 | 중 | WeasyPrint, OFL 한글 폰트 |
| 11 | collection-monitoring | 수집 경보, 장애 기록, 정기 점검, `/v1/ingest-status` | 06, 07, 08 | 가능 (하드웨어 감시 제외) | T07 | 중 | Prometheus |
| 12 | open-api-collector | 공공 관측 수집, 환경 결합 | 03, 07, 11 | 모의 어댑터로 가능 | T17 | 소 | — |
| 13 | sharing-governance | 공유 규칙, 심사, 데이터셋, 이용 신청, 배포 | 03, 08, 09 | 가능 | T01, T06, T08, T15 | 대 | — (정책 엔진 안 씀, ADR-0010) |
| 14 | deidentification-tagging | 선별 비식별, 검수, 태깅, 장면 검색 | 05, 13 | 가능 (CPU) | T06, T09, T16 | 대 | OpenCV, ONNX Runtime |
| 15 | edge-collectors | 엣지 캡처 플러그인 | 06 + 연계 카드 | 요구사항 1·2만 | T02, T03 | 중 | MediaMTX(PoC) |
| 16 | ngsi-ld-adapter | NGSI-LD·광역 허브 | 도입 게이트 | 조건부 | T19 | 중 | — |
| 17 | acceptance-release | 수용시험 실행, 부하 시험, Runbook, 릴리스 게이트 | 01부터 병행 | 가능 (실측은 하드웨어 후) | T01, T06, T12, T14, T18 | 중 | Locust, Playwright, ZAP |

- 17번은 01번과 함께 시작해 기능 스펙을 하나 끝낼 때마다 그 수용시험을 등록한다.
- 06번(에이전트)은 04번이 끝나면 05번과 병행할 수 있다. 서버와 에이전트를 같은 사람이 만들면 순서대로 한다.

**계약 먼저:** `contracts/`에 API·데이터 계약 초안이 들어 있다. 12월 G1 동결 회의에서 검토하고, 01~04번 구현은 동결본을 기준으로 한다. 동결 뒤 변경은 schema_version을 올려서만 한다.

## 2. 일정과 사업 게이트

| 마일스톤 | 포함 | 사업 게이트 | 완료 기준 |
|---|---|---|---|
| M0 계약 동결 | `contracts/` 초안 검토·수정 | G1 표준·계약 동결 (2026.12) | 협의체·기업 기술 담당이 스키마·API 문서에 합의 |
| M1 앵커 PoC 준비 | 01~12 (12는 모의) + 17 기본 (+ 필요하면 15번 요구사항 1~3) | G2 앵커 PoC (2027 Q1) | 앵커 기업 장비에서 사람 개입 없이 수집 + 검색 + 증빙·보고서 초안 |
| M2 운영 개통 | 하드웨어 반영 작업 (5장) | G3 기업 개통 (실증 개시 전) | 운영 스토리지·서버에서 M1 범위 세부 케이스(T01-a~f, T02-a~d·g·h, T03, T04, T05, T06-a·e, T07, T10, T12) 통과 |
| M3 공동활용 | 13, 14, 15 | G4 가공·검수, G5 승인형 공급 | 외부 이용기업 1곳 이상 실제 공급 |
| 조건부 | 16 | 도입 게이트 | 상대 시스템과 적합성 시험 통과 |

**앵커 PoC와 15번:** G2에서는 폴더 에이전트(NVR 내보내기 폴더 감시 포함)로 시작한다. 연계 카드에서 앵커 기업이 엣지 캡처(RTSP 녹화·현장 클립)를 꼭 써야 한다고 확인되면 15번 요구사항 1~3을 M1로 당긴다.

**조기 실증 경로:** 2027년 1월에 실증이 바로 시작되면 기준 문서 결정대로 G3를 축소 개통한다. 최소 범위는 02(권한)·04(업로드)·06(에이전트)·09의 기본 대시보드이고 01·03이 선행이다. 앵커 기업에 센서 기업이 있으면 07을 넣는다.

## 3. 하드웨어가 아닌 입력을 기다리는 것

| 입력 | 받으면 고칠 곳 | 그 전까지 |
|---|---|---|
| 기업별 데이터 연계 카드 | 장비 정책 기본값, local_copy_policy, 코드표(관측 항목·단위), 07번 매핑, 15번 확정 | 합성 데이터·모의 장비 |
| 센서 기업의 통신 방식 | 07번 MQTT 플러그인 또는 Telegraf PoC 결정 | REST 배치·CSV |
| 관리기관 실제 보고서 양식 | 10번 Report_Template | 범용 템플릿 |
| 법률 자문 (위탁·제공, 가명·익명, 2단 약정 허용 목적) | 13번 심사 문구, 약정 기록 항목 | 흐름과 상태만 구현 |
| 공공 Open API 명세·이용조건 | 12번 실제 어댑터 | 모의 어댑터 |
| NGSI-LD·광역 허브 의무 여부 | 16번 착수 여부 | DCAT 내보내기로 대비 |
| 전자정부 표준프레임워크 요구 여부 | tech.md 스택 | Python/FastAPI |

## 4. 먼저 해 볼 PoC
`docs/OSS-CANDIDATES.md` 4장 순서대로: procrastinate(05) → Frictionless(07) → 백업 도구(08) → Uppy·Refine(09) → MediaMTX(15) → Telegraf(07, 센서 기업 확인 뒤). 각 PoC는 해당 스펙을 시작하기 전에 끝낸다.
결과는 `docs/adr/`에 남긴다.

## 5. 하드웨어가 들어온 뒤 할 일
스토리지 SW·제품 선정 시점은 G2다 (기준 문서 Q18). 그 전까지 개발은 S3 호환 API만 가정한다.

1. tech.md "결정 대기 항목"(운영 스토리지·배포 형태)을 갱신한다.
2. 04·08번 통합 시험을 운영 스토리지로 다시 돌린다 (멀티파트·사전 서명·수명주기 차이 확인). Hot/Warm 계층 설정을 넣는다.
3. GPU 워크스테이션에서 `make bench`로 처리량을 재고 정책 테이블의 샘플링·우선순위를 조정한다 (T16).
4. 운영 배포 스펙(18번)을 새로 만든다: 배포 형태, 백업 대상, 모니터링, 회선·방화벽.
5. 11번 하드웨어 감시 어댑터(디스크·온도·전원)를 붙인다.
6. 17번 부하 시험 도구를 운영 대상으로 돌린다 (T12, T18).
