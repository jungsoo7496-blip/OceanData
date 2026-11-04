# 변경 내역

## v0.2 (2026-11-04)

### 스펙 번호를 진행 순서로 다시 매김
| v0.1 | v0.2 | 비고 |
|---|---|---|
| 01~04 | 01~04 | 그대로 |
| 06 processing-pipeline | 05 | |
| 05 sync-agent | 06 | |
| — | **07 sensor-event-ingestion** | 신설 |
| 07 storage-lifecycle | 08 | |
| 08 workspace-portal | 09 | 증빙·보고서를 10으로 분리 |
| — | **10 evidence-reports** | 신설 (08에서 분리 + 보강) |
| 09 collection-monitoring | 11 | |
| 12 open-api-collector | 12 | 그대로 |
| 10 sharing-governance | 13 | |
| 11 deidentification-tagging | 14 | |
| 13 edge-collectors | 15 | |
| 14 ngsi-ld-adapter | 16 | |
| — | **17 acceptance-release** | 신설 (T12·T14·T18, 릴리스 게이트) |

### v0.1의 누락·오류 수정
- **센서 IoT 수집(ING-08, 기준선 P0)이 빠져 있었다** → 07번 신설 (REST 배치, CSV 파싱, MQTT 에이전트 플러그인, 실증 이벤트).
- **RAW 백업 규칙이 엣지 순환 녹화와 충돌했다** → 장비별 local_copy_policy, backup_class=PROTECTED 승격 규칙 (domain.md 9장, ADR-0008).
- **Hot/Warm 계층(STO-04, P0)이 요구사항에 없었다** → 08번 요구사항 9.
- **연계 카드·온보딩(ONB-01·02)과 기관별 자원 사용 리포트(OPS-04)가 연결되지 않았다** → 03·09·08번.
- 같은 Idempotency-Key에 다른 본문이 오는 경우가 정해지지 않았다 → 409 IDEMPOTENCY_CONFLICT.
- 목록·검색 결과 건수로 다른 기관 데이터가 드러날 수 있었다 → security.md, 02번 요구사항 2.5.
- 이용 신청 ID 접두사 `req_`가 요청 추적 ID와 헷갈렸다 → `acr_`.
- T12·T14를 맡은 스펙이 없었다 → 17번.

### 독립 검토에서 고친 것 (패키지 완성 직전, 25건 중 반영)
- **공유 모델의 구멍 두 개를 막았다.** CORE 원본의 파생본이 L2로 갈 수 있었고, 운영자 최종 승인 없이 L2가 될 수 있었다 → 파생본은 원본의 가장 엄격한 data_role·sensitivity_class를 물려받고, 원본(ORIGINAL)은 L1을 넘지 않으며, L2에 share_status=APPROVED를 넣었다. 비식별 결과·검수 결과는 파생본에만 기록한다. L4는 L2 조건을 모두 포함하도록 강화했다.
- **역할별 데이터 범위 표**(domain.md 2장)를 만들었다. 운영자·검수자·이용기관·관리기관이 다른 기관 데이터를 어디까지 보는지 정했고, 관리기관 열람자에게는 어떤 등급이든 원본·프록시를 주지 않는다.
- **상태 전이표**(주체 포함)를 domain.md 5장에 넣었다. C0 자산이 나중에 원본을 받는 경로, 비영상 자산의 READY_PRIVATE 경로, 공유 철회 경로를 정했다.
- **수집기와 원천 장비를 구분**했다. 에이전트 하나(수집기)가 원천 장비 여러 대를 맡고, 정책·하트비트·자격증명이 이 구조를 따른다.
- **collection_mode별 허용 variant 표**와 업로드 매니페스트의 variant 필드를 넣어 C1·C2를 실제로 강제할 수 있게 했다.
- 격리 보존을 사유별로 나눴다 (회차 미상·메타 누락은 자동 삭제하지 않음). 쿼터 계산식을 하나로 정했다. 엣지 순환 삭제는 캡처 플러그인 파일에만 허용했다.
- 관측값 중복 키에 수심·채널을 넣었고, 공공 관측소는 원천 장비로 등록한다. 스키마의 배치 한도를 정책값으로 뺐고, 스키마 버전 확인 순서를 정했다.
- 하향 신청 경로, API 키 범위, 회차-장비 연결, 수집 지연 필드를 계약에 더했다. 에이전트 지원 API·쿼터 예약·무결성 로그 추가를 04번으로 옮겨 스펙 간 소유를 정리했다.
- 수용시험 담당 스펙을 ACCEPTANCE.md 하나에서 관리하고 각 스펙의 추적 절과 로드맵을 여기서 생성한다. 추적 검사가 둘이 다르면 실패한다.

### GPT v0.05에서 가져온 것
- 계약 파일 형식: OpenAPI 3.1 + JSON Schema + 예시 + 검증 스크립트 (내용은 domain.md 기준으로 새로 씀)
- RFC 9457 오류 형식, IDEMPOTENCY_CONFLICT·INVALID_STATE_TRANSITION·STORAGE_UNAVAILABLE 등 코드 (ADR-0005)
- 업로드 시 쿼터 예약, 업로드 취소, 기능 상태 API, 매니페스트 사전 검증 API, 보존 삭제 dry-run
- 보고서: 자동값·수정값 구분, 근거 손상 감지, "정답 없이 모델 결과만이면 검증 필요"
- 수용시험 AT-01~30을 T01~T20 세부 케이스로 편입 (docs/ACCEPTANCE.md)
- AGENTS.md, ADR 양식, 위험 기반 백업 지적

### GPT v0.05에서 가져오지 않은 것 (이유는 ADR)
- 로컬 파일 스토리지 어댑터 (ADR-0001), Redis 큐 + outbox (ADR-0002), GPU 없으면 비식별 UNAVAILABLE (ADR-0003), Python 에이전트 (ADR-0004)
- 미리 채운 design.md·tasks.md (ADR-0007) — 작업 112개 중 110개가 번호 순서로만 요구사항에 연결되어 있었다
- GPT의 데이터 모델 (data_role·sensitivity_class 없음, 공유 범위 4단계, 가명처리 상태 없음) — 기준선 domain.md 유지

### domain.md 변경
- L2·L4 조건에 "제공기관 2단 약정 유효" 추가, 이용 목적은 약정 허용 목적 안에서만 (기준 문서 14.1장)
- report_status, quarantine_reason, backup_class, local_copy_policy, area_code 추가
- timestamp_quality.source에 MANUAL(사람이 입력한 시각) 추가
- 관측값 규칙: 오프셋 없는 시각, 결측(null ≠ 0), 위치 미상, 중복·충돌
- 모델 결과는 human_verified=false, 정답·예측 구분
- 허용되지 않은 상태 전이 = INVALID_STATE_TRANSITION

### 오픈소스 재사용
- 사용자 정리표 59건을 v0.2 기준으로 판정하고 후보를 더함 (docs/OSS-CANDIDATES.md)
- tech.md: 재사용 우선 규칙, 라이선스 허용 목록에 Zlib·OFL(폰트) 추가, 계약 → 코드 생성 도구, 보안·공급망 도구

## v0.1 (2026-11-04)
- 기준 문서 v0.04를 steering 7개 + 스펙 14개로 변환
