# 설계: 11 수집 정상성 모니터링·알림·정기 점검

## 이번에 구현한 것 (마이그레이션 0008)
| 구성 | 구현 |
|---|---|
| 장비 활동 | `source_activity`: 장비별 마지막 정상 등록·마지막 관측·관측 수신·시계 오차. 원본 등록(`append_integrity`, 파생본 제외)과 관측값 저장(`ingest_items`) 때 갱신해 큰 표(무결성 로그·관측값 파티션)를 훑지 않는다. 마이그레이션에서 기존 자료로 채운다 |
| Health_Monitor | `monitoring.device_health()`: 수집기는 하트비트 지연·대기·오류·적용/현재 정책 버전(`policy_mismatch`)·시계 오차·디스크 여유, 원천 장비는 마지막 등록/관측·지연·다음 예상 수신(`expected_interval_s`). 관측값 장비의 지연은 마지막 관측 기준. `GET /v1/agents/{id}/health`도 같은 함수를 쓴다 |
| Watch_Rule_Engine | `watch_rules` 정책 표(13개 규칙, 임계값 `params`, 심각도, 대상(기관·운영자), 채널, 재알림 간격). 계산 방법만 코드(`EVALUATORS`)에. 신호원이 없는 규칙(OPENAPI_FAILURE → 12번)은 `data_source_ready=false`로 보이고 평가하지 않는다 |
| 경보·장애 | `watch_alerts`(규칙+대상 상태), `incidents`(`inc_`). 생성·재알림·해제·자동 해결 규칙은 ADR-0017 |
| Alert_Dispatcher | 메일: `SMTP_HOST`가 있으면 `SmtpNotifier`(개발은 mailpit), 없으면 로그. 웹훅: `ALERT_WEBHOOK_URL`(운영자 채널, `ALERT_WEBHOOK_SECRET` 있으면 HMAC 서명). 수신 주소는 `users.contact_email`. 본문은 요약·장애 번호·조치 안내만 (토큰·URL·개인정보 없음, 시험으로 확인) |
| API | `GET /v1/ingest-status`(장비별 상태 + 열린 장애·조치 안내 + 최근 24시간 실패 원인 상위, 기관 범위는 인증 정보로만), `GET/PATCH /v1/incidents`, `GET/PATCH /v1/watch-rules`(운영자, 수정은 MFA·감사), `POST /v1/hardware-signals`, `/v1/inspections*` |
| 정기 점검 | 템플릿은 기준 문서 15.2장(`domain/inspections.py`). 기간 키(일 `2026-11-03`, 주 `2026-W45`, 월, 분기 `2026-Q4`, 연) → 기한(운영 시간대 `OPS_TIMEZONE` 기간 끝). 생성 시 자동 확인 항목 채움: 수집 성공/실패·격리·저장 사용률·하드웨어·SMART·GPU 적체·해시 표본(`verify_integrity`)·보존 실행(`daily_tasks`)·용량 예측. 백업 성공은 08번 백업이 없어 `AUTO_UNAVAILABLE`. 증빙 파일은 evidence 버킷 `ops/inspections/{id}/` 사전 서명 PUT. 하루 한 번 기한 지난 바로 앞 기간을 운영자에게 알림 |
| Hardware_Signal | `hardware_signals`(호스트·종류·구성요소별 최신 상태). `POST /v1/hardware-signals`(운영자) 또는 `python -m app.cli hardware-signal`(모의 신호). 규칙 HARDWARE_SIGNAL이 `min_status` 이상·`stale_s` 안 신호로 경보 |
| 메트릭 | 워커(`:9101`): `ingest_lag_seconds{organization_id,device_id,kind}`, `watch_alerts_active{rule_code}`, `watch_evaluations_total`, `incidents_opened_total{rule_code}` |
| 화면 | 기업 대시보드: 장비별 수집 상태(한 번 호출), 경보와 조치 안내, ORG_ADMIN 확인·처리 기록. 운영 콘솔 `/monitoring`: 지연 큰 순 표(기관 필터는 화면에서), 실패 원인 상위, 열린 장애, 감시 규칙, 정기 점검(어제 일일 점검 시작·결과 기록·완료) |

## 규칙별 계산 (기본 임계값은 마이그레이션 0008, 운영자가 조정)
| 규칙 | 대상 | 위반 조건 |
|---|---|---|
| HEARTBEAT_MISSING | 수집기(ACTIVE) | 마지막 하트비트(없으면 자격증명 발급) 후 max(factor × 정책 heartbeat_interval_s, min_silence_s) 초과 |
| INGEST_LAG | 원천 장비(관측값 아님, 한 번 이상 수신) | 마지막 정상 등록 후 factor × expected_interval_s 초과 |
| OBSERVATION_LAG | 관측값 장비 | 마지막 관측 후 factor × expected_interval_s 초과 |
| SESSION_NO_DATA | 회차(장비 지정) | 시작 + grace_s 지남, 최근 window_s(긴 회차는 이 기간마다) 동안 지정 장비에서 자산·관측값 0건. 회차 미상 자산도 유입으로 센다 |
| VOLUME_DROP | 기관 | 최근 24시간 원본 수 < 지난 baseline_days 하루 평균 × drop_pct% (평균이 min_baseline_daily 이상일 때) |
| UPLOAD_FAILURE_RATE | 기관 | window_s 안 업로드 min_uploads건 이상, (EXPIRED·오류 코드) 비율 > max_failure_pct |
| PROCESSING_BACKLOG | 작업 유형 | QUEUED이고 run_after가 max_wait_s 넘게 지난 작업 (운영자만) |
| QUARANTINE_SURGE | 기관 | window_s 안 격리 min_count건 이상이고 평소 같은 길이 평균의 factor배 초과 |
| STORAGE_FORECAST | 기관 | 남은 쿼터 ÷ 최근 trend_days 하루 평균 증가 < days_left (또는 이미 초과) |
| CLOCK_OFFSET | 수집기·원천 장비 | 하트비트·최근 window_s 안 관측 배치의 시계 오차 절댓값 > max_offset_ms |
| LOCAL_DISK_LOW | 수집기 | 하트비트 disk_free_bytes < min_free_bytes |
| HARDWARE_SIGNAL | 호스트/종류/구성요소 | stale_s 안 신호 상태 ≥ min_status (운영자만) |
| OPENAPI_FAILURE | — | 12번에서 신호원 연결 (지금은 평가 안 함) |

## 결정
- ADR-0017: 경보 상태·장애 자동 해결·ACK 시 재알림 중지·수신 주소(`contact_email`)
- 기관 범위는 쿼리로 바꾸지 않는다(security.md). 운영자는 전체를 받아 화면에서 거른다 — 처음 설계한 `organization_id` 쿼리는 빼고 시험으로 400 확인

## 확인 필요 / 다음 스펙과 연결
- 임계값 기본값(하트비트 3배·최소 5분, 지연 3배, 회차 2시간 유예·48시간 창, 현장 디스크 10GiB 등)은 시범 운영 후 운영기관이 정한다
- 운영 메신저(카카오워크·Slack 등) 채널: 결정 대기. 지금은 메일 + 일반 웹훅
- 하드웨어 신호 수집 어댑터(노드 익스포터·SMART·UPS): 하드웨어 확정 후. 외부 수집기용 기계 자격(지금은 운영자 사용자 토큰만)도 그때
- 기관 웹훅 구독으로 경보 보내기: 04번 웹훅 구독 기능이 생기면 이벤트 유형 추가 (계약 webhook-event 변경 필요)
- 사용자 `contact_email`을 IdP(Keycloak) 이메일과 맞추는 방법: 02번 사용자 관리와 함께
- 점검 증빙 파일 내려받기 화면, 점검 항목 템플릿 편집: 필요해지면
