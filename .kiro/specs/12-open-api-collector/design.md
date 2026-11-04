# 설계: 12 공공 Open API 수집·환경 정보 결합

## 이번에 구현한 것 (마이그레이션 0013)
| 구성 | 구현 |
|---|---|
| Source_Registry | `external_sources`: 이름·어댑터 종류·엔드포인트·인증키 **참조**(비밀 저장소·환경 변수 이름, 값 아님)·주기·한 번에 가져올 최대 구간·호출 한도·과거 조회 지원·관측소(코드·이름·위경도)·필드 매핑(관측소·시각 경로, 항목별 경로·단위)·출처 표기·재배포 허용·수집 기준점·연속 실패. `GET/POST/PATCH /v1/external-sources`(운영자) |
| 명세 미확인 | 어댑터는 `MOCK`만 등록할 수 있고 `spec_status=NEEDS_CONFIRMATION`. 실제 기관 API는 명세·이용조건을 확인한 뒤 어댑터를 추가한다 (지어내지 않음) |
| Source_Adapter | `adapters/external_sources.py` `MockOceanSource`: 10분 간격 합성 값(재현 가능), 과거 조회 지원, 형식 변경 모의 옵션. 응답 형식은 시험용으로 정한 것이며 실제 API와 무관 |
| Collection_Scheduler | 워커 정비 주기 `external.schedule`: 소스 주기가 되면 `collect_source` 작업(수집 기준점부터 `max_window_s`까지 → 빈 구간이 있으면 자동 보충, 과거 조회 미지원 소스는 지금 구간만). 실패는 작업 큐의 지수 백오프. 호출 한도는 한 번에 부를 관측소 수로 제한. `POST …/backfill`(운영자, 과거 31일 안, 구간을 작업으로 나눔) |
| 형식 변경 | 필드 매핑과 안 맞으면 원 응답을 QUARANTINE 버킷 `external/…`에 보관, 연속 실패·형식 오류 시각 기록 → 11번 `OPENAPI_FAILURE` 규칙(이제 신호원 연결됨)이 경보·장애, 정상 수집되면 해제 |
| 저장 | 관측소마다 운영기관 소유 원천 장비(`EXTERNAL_STATION`) 자동 생성. 07번 `ingest_items`로 저장(단위 사전 정규화·원래 값·단위 보존), 원 응답은 RAW 버킷에 두고 관측값 `source_ref`에 참조(마이그레이션이 분할 표 `observations`에 열 추가) |
| Env_Joiner | `external.join_env`: 촬영 구간 중간 시각·Point 위치 기준 `ENV_JOIN_RADIUS_KM`(30) 안 관측소, `ENV_JOIN_WINDOW_MIN`(±30분) 안에서 항목별로 가장 가까운(거리 우선, 다음 시각 차이) 관측값 → `env_context.observations[]`에 값·단위·관측소·거리(km)·시각 차이(초)·출처 표기·재배포 허용. 촬영시각 미상이면 결합 안 함. 등록 시(촬영시각·위치가 있으면)와 영상 메타 추출 뒤 `env_join` 작업, 수집·보충 뒤 겹치는 자산 다시 계산 |
| 재배포 제한 | 자산 응답에서 기관 밖 주체(운영자·관리기관·다른 기관)에게는 재배포 허용 관측값만 남긴다(`public_env`). 데이터셋 매니페스트·DCAT에는 env_context를 넣지 않는다 |
| 출처 표기 | 자산 상세 '촬영 당시 환경' 표 아래 출처, 보고서 변수 `{{env.attribution}}` |
| 보고서·검색 | 보고서 변수 `env.<항목>.max`·`env.<항목>.mean`·`env.attribution`(회차·기간 자산의 env_context 요약). 자산 목록 `env_metric`·`env_min`·`env_max`(JSONB 조건) 필터, 화면 필터 |
| 화면 | 운영 콘솔 '수집 모니터링'에 소스 상태(명세 확인 필요 표시·기준점·연속 실패·재배포·오류), 자산 상세 환경 표, 자산 목록 환경 필터 |

## 확인 필요
- 실제 공공 기상·해양 Open API(기관·엔드포인트·인증·호출 한도·이용조건·재배포 조건) — 확인 후 어댑터와 `spec_status=VERIFIED`
- 반경·시간 창 기본값(30km·±30분)은 초안 — 해역·항목별로 다를 수 있다
- 촬영 위치가 경로(LineString)인 자산은 지금 결합하지 않는다 (Point만)
- 14번 날씨 태그 보정(요구사항 6.3)은 env_context가 생겼으므로 다음 단계에서 연결
