# 작업: 11 수집 정상성 모니터링·알림·정기 점검

- [x] 1. 장비별 마지막 하트비트·정상 등록·대기·오류·버전 집계, `source_activity` 갱신 — 요구사항 1.1 · 시험 `test_t07a_*`, `test_t07c_*`
- [x] 2. 기관·장비별 수집 지연, 기대 주기로 다음 예상 수신, 관측값 장비는 마지막 관측 기준 — 요구사항 1.2, 1.3 · 시험 `test_t07c_observation_lag_and_clock_offset`
- [x] 3. 적용·현재 정책 버전 차이(`policy_mismatch`)와 시계 오차 표시 — 요구사항 1.4
- [x] 4. 감시 규칙 정책 표(13개), 규칙별 대상·심각도·채널·재알림 간격, 운영자 조정 API(MFA·감사·임계값 검사) — 요구사항 2.1, 2.2 · 시험 `test_watch_rules_api`, `test_validate_params`
- [x] 5. 규칙 계산: 하트비트·수집 지연·회차 무수집·수집량 급감·업로드 실패율·처리 대기·격리 급증·용량 예측·관측값 지연·시계 오차·현장 저장 부족·하드웨어 — 요구사항 2.1 · 시험 `test_processing_upload_quarantine_storage_rules`, `test_volume_drop`, `test_t07b_*`
- [ ] 6. 외부 Open API 수집 실패 규칙의 신호원 — 요구사항 2.1 (12번에서 연결, 지금은 `data_source_ready=false`)
- [x] 7. 메일(SMTP)·운영자 웹훅 알림, 재알림 간격, 해제 알림, 본문에 토큰·URL·개인정보 없음 — 요구사항 3.1~3.4 · 시험 `test_t07a_*`, `test_webhook_channel_body`
- [ ] 8. 운영 메신저 채널, 기관 웹훅 구독으로 경보 보내기 — 요구사항 3.1 (메신저 결정 대기, 04번 웹훅 구독 이후)
- [x] 9. 경보 → 장애 자동 생성, 확인·해결·담당자·메모, 기관·장비·기간 조회 — 요구사항 4.1, 4.2 · 시험 `test_t07a_*`, `test_T01_a_cross_org_incident_update_and_list`
- [x] 10. 운영 콘솔 수집 지연 표·실패 원인 상위, 기업 화면 경보·조치 안내 — 요구사항 5.1, 5.2
- [x] 11. `GET /v1/ingest-status` (ORG 역할·기관 API 키, 기관 범위는 인증 정보로만) — 요구사항 5.3 · 시험 `test_t07a_*`, `test_processing_upload_quarantine_storage_rules`
- [x] 12. 정기 점검 템플릿(15.2장)·기간·기한, 수행 결과·특이사항·증빙 파일 — 요구사항 6.1, 6.2 · 시험 `test_inspections_flow`, `test_period_*`
- [x] 13. 기한 지난 점검 알림 (하루 한 번) — 요구사항 6.3 · 시험 `test_inspections_flow`
- [x] 14. 자동 확인 항목 채우기 (백업 성공은 08번 백업 전까지 AUTO_UNAVAILABLE) — 요구사항 6.4
- [x] 15. 하드웨어 신호 수신 인터페이스·규칙 연결·모의 신호(API·CLI) — 요구사항 7.1, 7.2 · 시험 `test_hardware_signal_mock_and_rule_disable`
- [ ] 16. 실제 하드웨어 어댑터(노드 익스포터·SMART·UPS)와 외부 수집기용 기계 자격 — 요구사항 7.1 (하드웨어 확정 후)
- [x] 17. Prometheus 메트릭(장비별 지연·규칙별 활성 경보·평가·새 장애) — 요구사항 8.1
- [x] 18. T07 시험: 에이전트 중지 → 기준 시간 안 경보·장애, 회차 무수집, 관측 지연·시계 오차, 기업 화면 안내 — 요구사항 8.2 · 시험 `tests/acceptance/test_t07_collection_watch.py`
