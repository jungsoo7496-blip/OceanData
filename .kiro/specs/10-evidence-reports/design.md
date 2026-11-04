# 설계: 10 실증 기록·증빙·보고서

## 이번에 구현한 것 (마이그레이션 0009)
| 구성 | 구현 |
|---|---|
| 회차 기록 | `session_records` + `session_record_versions`. `POST /v1/sessions/{id}/records`(멱등 키), `PATCH /v1/records/{id}`는 **항상 새 버전**(이전 버전·승인본 보존), `GET …/versions` 이력. 근거(`evidence_refs`: 자산 구간·이벤트·관측 구간)는 같은 기관·존재 여부를 확인하고, 다른 기관 근거는 '없음'과 같은 오류(존재 비공개) |
| 증빙 승인 | `POST /v1/records/{id}/approve`(ORG_ADMIN): 최신 버전 APPROVED·승인자·시각. 인용 자산(자산 근거 + 이벤트에 연결된 자산 + 관측 구간의 원천 파일 자산) → `backup_class=PROTECTED`. 클립을 인용했으면 그 클립만 |
| 인용 역색인 | `evidence_citations`(기록·보고서 버전 → 자산). 근거 손상 감지와 08번 `retention.affected_by()`(승인된 기록·승인/제출 보고서가 인용한 자산은 삭제 요청 시 영향 목록과 함께 409, 만료 실행에서 제외)에 쓴다 |
| 지표 | `metric_definitions`(코드·버전, 운영자 등록). 계산 방법 4종: EVENT_COUNT(상위분류는 하위코드 포함), EVENT_RATIO, OBSERVATION_STAT(mean/min/max/count, BAD·MISSING 제외 → 누락 건수), VIDEO_HOURS(원본 영상 길이, 길이 모름 → 누락). 결과마다 계산식·정의 버전·입력/누락 건수·원천(이벤트·자산·관측 구간) 저장 |
| 데이터 없음·검증 필요 | 입력이 없으면 값 null·NO_DATA (0과 '기록 없음'을 가른다: 이벤트 0건이어도 범위에 자료가 없으면 NO_DATA). 사용한 이벤트가 모두 MODEL·미확인이면 NEEDS_VERIFICATION — 내보내기에서 숫자 대신 '검증 필요' |
| 보고서 양식 | `report_templates`(코드·버전, 운영자). 절: TEXT(`{{session.label}}` 등 변수), FIELDS(입력 항목, 자동값 식), METRICS(지표 코드 — 등록된 것만), RECORDS(승인된 회차 기록). 범위 SESSION·PERIOD. HWPX 원본은 `PUT …/versions/{v}/hwpx`로 첨부(EVIDENCE 버킷 `ops/report-templates/`) |
| 보고서 | `reports` + `report_versions`(report_status는 domain.md 5장 표로만 전이). 초안 생성 시 지표 계산·승인된 기록 연결·인용 기록. 입력 항목 수정은 자동값을 두고 수정값·수정자·시각·이전 값 이력. 새 버전(`POST …/versions`)은 지금 데이터로 다시 계산 |
| 승인·제출 | 승인(ORG_ADMIN): 버전 고정, 정렬된 JSON 스냅숏을 EVIDENCE 존 `{기관}/reports/{id}/v{n}.json`에 저장·sha256 기록·기관 사용량 반영, 인용 자산 PROTECTED, 같은 보고서의 이전 승인·제출본 SUPERSEDED. 제출: APPROVED만, 근거 손상 있으면 422(원인 자산 목록). 제출해도 자산 sharing_scope는 그대로 |
| 관리기관 열람 | PROGRAM_VIEWER는 배정 사업의 **제출된 적 있는 버전**만 목록·조회·내보내기·추적 (다른 버전 요청은 404). 회차 기록 API는 볼 수 없다 |
| 근거 손상 | Evidence_Integrity_Checker(`evidence.check_damage`, 워커 감시 주기): 인용 자산이 삭제·격리됐거나 인용 뒤 재처리되면(`assets.last_reprocessed_at`, 재처리 API가 원본과 파생본에 표시) 해당 버전에 `evidence_damaged`·원인 목록, 작성자와 ORG_ADMIN에게 메일. 인용 한 건은 한 번만 표시 |
| 역추적 | `GET /v1/reports/{id}/metrics/{code}/trace`: 지표 → (원천과 겹치는) 회차 기록 → 이벤트·관측 구간 → 자산 → 계보 원본. 권한 범위 밖 자산은 숨기고 수만(`hidden_count`) |
| 내보내기 | `GET …/export?format=PDF|XLSX|HWPX` — `REPORT_FORMATS`에 켠 형식만(`/v1/system/capabilities`). PDF는 WeasyPrint(HTML), XLSX는 openpyxl(보고서·지표·회차 기록 시트), HWPX는 양식 zip 안 XML의 `{{변수}}` 치환. 모든 형식에 상태(초안 등)·생성 시각·데이터 기준 시각 (ADR-0018) |
| 화면 | 보고서 목록·초안 만들기(회차 양식), 상세(지표 상태·계산식·입력/누락, 행 클릭 시 근거 경로 서랍, 입력 항목 고치기·이력, 승인·제출·새 버전·내보내기), 회차별 기록 화면(작성·근거 자산 선택·증빙 승인). 관리기관 열람자 메뉴 '제출된 보고서' |
| 예시 데이터 | 시드에 예시 지표 4개·예시 양식 `EXAMPLE_SESSION_LOG`(합성, 이름에 '예시'). 실제 실증지표·관리기관 양식은 확보 후 운영자가 API로 등록 |

## HWPX 변수
`{{session.label}}`·`{{session.title}}`·`{{session.starts_at}}`·`{{project.name}}`·`{{organization.name}}`·`{{period.from}}`,
`{{report.title}}`·`{{report.status}}`·`{{report.version}}`·`{{report.generated_at}}`·`{{report.data_as_of}}`,
`{{field.<key>}}`, `{{metric.<CODE>}}`(값·'데이터 없음'·'검증 필요'), `{{metric.<CODE>.formula}}`. 모르는 자리표시자는 그대로 둔다.

## 결정
- ADR-0018: 보고서 내보내기 방식, Pillow·Pyphen 라이선스 예외(승인 대기)
- 기록 수정은 승인 여부와 관계없이 항상 새 버전 — "승인된 증빙은 새 버전으로만 변경"과 "수정 전 내용 보존"을 한 규칙으로 만족
- 보고서 승인 스냅숏은 JSON(정렬·고정 형식). PDF 등은 같은 데이터에서 언제든 다시 만들 수 있다

## 확인 필요 / 다음 스펙과 연결
- **실제 관리기관 보고서 양식(T11-f)**: 확보 후 Report_Template·HWPX로 등록하고 한글 프로그램에서 열리는지, 자리표시자가 텍스트 조각으로 나뉘지 않는지 확인
- 실증지표 정의(분자·분모·대상 기간)는 관리기관·기업과 합의 후 등록. 지금 4가지 계산 방법으로 표현 못 하는 지표가 나오면 kind를 추가
- 정답 데이터(annotation_kind=PREDICTION 구분)로 검증 필요를 푸는 규칙: 14번 태깅과 함께
- 이용 권한이 있는 데이터셋 버전의 인용(affected_by): 13번
- PROTECTED 자산의 실제 독립 백업: 08번 백업 작업(PoC 후)
