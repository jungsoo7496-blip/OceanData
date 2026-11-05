# 작업: 04 수집 API

- [x] 1. 업로드 생성: 범위·정책·variant·쿼터·파트 URL — 요구사항 1.1, 1.2, 1.7 · 시험 `test_upload_happy_path_and_receipt`, `test_variant_rules_and_metadata_only`
- [x] 2. C0 메타데이터 전용 등록 — 요구사항 1.3 · 시험 `test_variant_rules_and_metadata_only`
- [x] 3. 쿼터 예약·확정·격리·해제 (행 잠금) — 요구사항 1.4, 1.5 · 시험 `test_quota_abort_and_expiry`, `test_T04_checksum_mismatch_is_quarantined`
- [x] 4. 쿼터 동시성 시험 (병렬 업로드 생성에서 합계 초과 없음) — 요구사항 1.5 · 시험 `tests/acceptance/test_t12_concurrency.py` (T12-a: 동시 12건·쿼터 5건분 → 정확히 5건, 예약+사용 ≤ 쿼터)
- [x] 5. 스토리지 장애 503 + Retry-After — 요구사항 1.6 (멀티파트 생성 실패 시. 자동 시험 없음)
- [x] 6. 멱등: ALREADY_REGISTERED, Idempotency-Key 재응답·충돌, C0 자산 세션 재개 — 요구사항 2.1~2.4 · 시험 `test_duplicates_and_idempotency`
- [x] 7. 재개·만료(410)·취소 — 요구사항 3.1~3.4 · 시험 `test_T03_resume_after_interruption`, `test_quota_abort_and_expiry`
- [x] 8. 버려진 세션 주기 정리 작업 (워커 5분 주기, SKIP LOCKED) — 요구사항 3.3 · 시험 `test_quota_abort_and_expiry`
- [x] 9. 완료 → RECEIVED → 무결성 검증 작업 — 요구사항 4.1, 4.2
- [x] 10. 해시 불일치·MIME 불일치·형식 미지원 격리, 매직 바이트 판별 — 요구사항 4.3~4.5 · 시험 `test_T04_*`
- [x] 11. 무결성 로그 해시 체인 + 영수증 — 요구사항 4.6, 7.1, 7.3
- [ ] 12. 악성코드 검사 어댑터 (제품 결정 대기, 포트·모의 구현) — 요구사항 4.7
- [x] 13. 격리 사유·격리 존 이동·사유별 보존 — 요구사항 5.1, 5.2, 5.4
- [x] 14. 회차 지정 → 재검증 → 등록 — 요구사항 5.3 · 시험 `test_unknown_session_quarantine_then_assign_and_revalidate`
- [x] 15. 회차 결정 규칙 — 요구사항 6.1, 6.2 (담당자 "분류 요청" 통지는 격리 통지로 대신 — 09번 화면에서 작업함으로)
- [x] 16. 업로드 상태(RECEIVED·REGISTERED 구분) — 요구사항 7.2
- [ ] 17. 웹훅 구독·서명 전송·재시도·비활성화 — 요구사항 8.1~8.3 (contracts `webhooks` 절 있음)
- [x] 18. 호출 제한 429 + Retry-After — 요구사항 9.1, 9.2 (주체별 분당, 자동 시험 없음)
- [x] 19. 응답 계약 시험 (UploadCreated·UploadStatus·PartList·Receipt·asset-metadata) — 요구사항 10.1
- [ ] 20. Python 예제 클라이언트 + Schemathesis CI — 요구사항 10.2
- [ ] 21. 10GB 끊고 재개 시험 (SeaweedFS) — 요구사항 10.3 (현재는 11MB 축소판)
- [x] 22. 손상 파일 격리 시험 — 요구사항 10.4
- [x] 23. 장비 정책 조회 — 요구사항 11.1 (03번에서 구현)
- [x] 24. 하트비트 저장·`GET /v1/agents/{id}/health` (상태: NEVER_SEEN/OK/STALE/ERROR, 지연은 11번) — 요구사항 11.2, 11.3 · 시험 `test_heartbeat_and_health`
- [x] 25. 자산 목록·상세(감사)·수정(data_role 사유·감사, 위치 정밀도) — contracts assets 태그
- [x] 26. 원래 좌표계 → EPSG:4326 (PostGIS ST_Transform) — 03번 요구사항 6.3 · 시험 `test_source_location_is_transformed_to_4326`

## 확인 필요 (사람 결정)
- 취소·만료된 C0 시작 세션의 UPLOADING → READY_PRIVATE 역전이를 domain.md 5장 표에 추가할지
- LineageEdge.relation에 SAMPLE_FRAME이 없다. 현장 샘플 프레임은 THUMBNAIL로 기록 중 — 계약에 값을 추가할지
