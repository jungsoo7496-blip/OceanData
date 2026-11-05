# 작업: 07 센서·이벤트 수집

- [x] 1. 관측값 배치 API·항목별 결과·400/413·Idempotency-Key — 요구사항 1.1~1.5 · 시험 `test_batch_item_results_and_rules`, `test_batch_errors_limit_scope_and_idempotency`
- [x] 2. 시간대·단위·결측·미래 시각·시계 오차 규칙 — 요구사항 2.1~2.6 · 시험 `test_observation_rules.py`
- [x] 3. 중복·값 충돌·정정 이력 — 요구사항 3.1, 3.2 · 시험 `test_aggregation_and_correction`
- [x] 4. 매핑 프로필 버전·정책 노출·수정 감사 — 요구사항 4.1~4.3 · 시험 `test_csv_parsing_path_matches_rest_and_reparse`
- [x] 5. 센서 파일 파싱·원본 연결·실패 기록·통지·재파싱 차이 — 요구사항 5.1~5.4 · 같은 시험
- [x] 6. MQTT Sensor_Plugin·오프라인 대기열·시간순 전송, Mosquitto 통합 시험 — 요구사항 6.1, 6.2, 6.4 · 시험 `TestMQTTPluginSubscribeQueueAndSendInOrder` (실제 토픽·페이로드는 연계 카드 후 확인 필요)
- [x] 7. 센터 MQTT 포트 미개방 (compose 브로커는 sensor 프로필의 시험용) — 요구사항 6.3
- [x] 8. 실증 이벤트 등록·코드 확인·자산 연결·MODEL 확인·중복 방지 — 요구사항 7.1~7.5 · 시험 `test_events`
- [ ] 9. 이벤트 구간과 겹치는 자산 조회 전용 API (지금은 자산 목록의 촬영 시각 필터) — 요구사항 7.6
- [x] 10. 관측값 조회·구간 집계(결측 분리)·이벤트 조회 — 요구사항 8.1~8.3
- [x] 11. 합성 관측값 생성기 — 요구사항 9.1 · `make sample-observations`
- [x] 12. 세 경로 같은 저장 결과 — 요구사항 9.2 · CSV↔REST 시험 + MQTT는 REST로만 전송
- [x] 13. 계약 예시 통과·거부 — 요구사항 9.3 (`make contracts-check`, 03번 검증기)
- [ ] 14. MQTT 계정 비밀번호를 OS 보안 저장소로 (지금은 config.json) — 06번 DPAPI와 함께
- [x] 15. 기관 하위 이벤트 코드 등록 API — 03번 tasks 12로 구현 (`POST /v1/codes/event-categories/subcodes`, ADR-0026) · 시험 `tests/integration/test_org_event_subcodes.py`
