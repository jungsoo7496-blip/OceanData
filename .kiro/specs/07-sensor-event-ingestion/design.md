# 설계: 07 센서·이벤트 수집

## 세 수집 경로, 하나의 규칙
```
REST 배치 POST /v1/observations ─┐
CSV·JSON Lines 자산 → sensor_parse ┼→ services/observations.ingest_items() → observations(월 파티션)
MQTT → 에이전트 Sensor_Plugin → 로컬 대기열 → REST 배치 ┘
```
`domain/observations.normalize()`가 시각·단위·결측·미래 시각·시계 오차 규칙을 한 곳에서 판단한다 (요구사항 9.2).

## 관측값
| 규칙 | 구현 |
|---|---|
| 시각 | observed_at은 오프셋 필수. 없으면 source_timestamp + 장비 source_timezone(zoneinfo)으로만 변환, 원문 보존. 둘 다 없으면 TIMEZONE_UNKNOWN |
| 미래 | 수신 시각 + OBSERVATION_FUTURE_TOLERANCE_S(600) 넘으면 FUTURE_TIMESTAMP |
| 결측 | value=null ⇔ MISSING, 어긋나면 MISSING_FLAG_MISMATCH |
| 단위 | units.json으로 정규화, 원래 값·단위 보존. 사전에 없거나 항목 단위(metrics.json)와 다르면 SUSPECT |
| 시계 오차 | clock_offset_ms 저장, CLOCK_OFFSET_WARN_MS(2000) 넘으면 GOOD→SUSPECT. observed_at은 그대로 |
| 항목 코드 | metrics.json에 없으면 UNKNOWN_METRIC |
| 중복 | (기관·장비·항목·시각·수심·채널) `UNIQUE NULLS NOT DISTINCT`. `INSERT … ON CONFLICT DO NOTHING RETURNING`으로 들어간 것을 판정, 나머지는 기존 값과 비교 → DUPLICATE / VALUE_CONFLICT. 배치 안의 중복도 같은 규칙 |
| 400 vs 항목별 | 시각·결측·항목 코드 문제는 항목별 REJECTED. 그 밖의 형식 오류는 배치 전체 400(아무것도 저장 안 함) |
| 한도 | 항목 수 > OBSERVATION_BATCH_MAX(5000)이면 스키마 검증 전에 413 |

조회: 키셋 커서(observed_at, 장비, 항목, 수심, 채널). `interval=PT{n}M|H`이면 `date_bin` 구간별 min·max·avg(결측 제외)·count·missing_count (metric 필수).
정정: `POST /v1/observations/corrections` (ORG_ADMIN, 사유) → `observation_corrections`에 이전 값 보존 + 감사.

## 매핑 프로필 (ADR-0015)
`observation_mappings` 버전 저장, `GET/PUT /v1/devices/{id}/observation-mapping`(ORG_ADMIN·운영자, 전후 감사), 장비 정책 `observation_mapping_version = m{버전}`. 개발용 `python -m app.cli set-mapping`.

## 센서 파일 파싱
SENSOR 자산(csv·json·ndjson)이 등록되면 `sensor_parse` 작업. 결과 `sensor_parse_runs`(상태 OK/PARTIAL/FAILED, 건수, 행별 사유 최대 1000, 이전 결과 차이). 매핑 없음·형식 불일치 → FAILED + 통지, 원본 자산은 READY_PRIVATE 유지. 다시 파싱은 `POST /v1/assets/{id}/reprocess {"steps":["SENSOR_PARSE"]}`.

## MQTT (에이전트, `agent/internal/sensor`)
paho.mqtt.golang(EDL). config.json `sensor_plugins`(브로커·client_id·QoS·계정). 매핑(format=mqtt_json, topic_filter)을 서버에서 받아 구독, JSON 페이로드 → 관측값 항목 → SQLite `observations_queue` → 원천 장비별 시간순 1000개 배치 전송. 오프라인이면 쌓아 두고 백오프. 형식 오류(400) 배치는 기록 후 버림(뒤 관측값이 막히지 않게). 하트비트 queue.observations·plugins 상태. 센터는 MQTT 포트를 열지 않는다 (compose `sensor` 프로필의 Mosquitto는 현장 브로커 대역).

## 실증 이벤트
`events`·`event_assets`. 코드: 상위분류(event-categories.json) + 기관 하위코드(org_event_codes) 확인. 연결 자산은 같은 기관만. MODEL은 human_verified=false 강제, PATCH로 사람만 확인(확인자·시각, 감사). 원천 이벤트 ID 중복이면 200으로 기존 기록.
겹치는 자산 조회(요구사항 7.6)는 `GET /v1/assets?session_id=&captured_from=&captured_to=`로 한다 (촬영 시작 시각 기준 — 구간 겹침 전용 API는 확인 필요).

## 계약 변경
매핑·정정 엔드포인트와 스키마, 재처리 SENSOR_PARSE, createEvent 422 추가.
`event.schema.json`: "MODEL은 생성 시 human_verified=false" 규칙을 event_body에서 요청 최상위로 옮김 — 응답 EventRecord가 사람이 확인한 모델 이벤트를 표현하지 못하던 문제 수정 (invalid 예시는 계속 실패).

## 실제 스택 확인 (2026-11-04)
- 합성 관측값 78건(`tools/sample_observations.py`) → 단위 오류 SUSPECT, 시간대 없음·미래 시각 거부, KST 원천 시각 변환, 결측, 중복 판정이 기대대로
- Mosquitto(compose `sensor` 프로필) → 에이전트 MQTT 플러그인 → 실제 API → 저장, -999는 null+MISSING
