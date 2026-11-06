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
- [x] 12. 악성코드 검사 어댑터 (포트·EICAR 모의 구현·기관 정책 플래그 0016·검사기 없으면 미룸) — 요구사항 4.7 · 시험 `tests/integration/test_malware_scan.py` · ADR-0031. **실제 검사기 제품은 결정 대기**
- [x] 13. 격리 사유·격리 존 이동·사유별 보존 — 요구사항 5.1, 5.2, 5.4
- [x] 14. 회차 지정 → 재검증 → 등록 — 요구사항 5.3 · 시험 `test_unknown_session_quarantine_then_assign_and_revalidate`
- [x] 15. 회차 결정 규칙 — 요구사항 6.1, 6.2 (담당자 "분류 요청" 통지는 격리 통지로 대신 — 09번 화면에서 작업함으로)
- [x] 16. 업로드 상태(RECEIVED·REGISTERED 구분) — 요구사항 7.2
- [x] 17. 웹훅 구독·서명 전송·재시도·비활성화 — 요구사항 8.1~8.3 · `/v1/webhooks/subscriptions`, outbox(0015)·워커 전송·SSRF 차단 · 시험 `tests/integration/test_webhooks.py` · ADR-0030
- [x] 17b. 보안 검토 후속 — 웹훅 전송 격리: 전용 스레드·잠금 없는 가져오기/기록·한 건 전체 기한(본문 안 읽음)·구독당 한 건·기관별 몫, 공인 주소만·검사한 주소로 고정·프록시 무시·거부 이유 통일 — 요구사항 8.2, 8.3 (재시도·백오프·비활성·서명 형식은 그대로) / 기관당 구독 상한 409(계약 먼저)·다른 기관 구독 삭제 감사 — 요구사항 8.1 · 시험 `tests/integration/test_webhooks.py`, `tests/unit/test_webhook_send.py` · ADR-0041. DB 구조 변경 없음(SENDING은 기존 status 열 값 — 새 마이그레이션·down 해당 없음)
- [x] 18. 호출 제한 429 + Retry-After — 요구사항 9.1, 9.2 (주체별 분당, 자동 시험 없음)
- [x] 19. 응답 계약 시험 (UploadCreated·UploadStatus·PartList·Receipt·asset-metadata) — 요구사항 10.1
- [x] 20. Python 예제 클라이언트(`examples/python`) + Schemathesis 계약 시험(CI backend 작업의 `pytest`) — 요구사항 10.2 · 시험 `test_example_client_interrupted_upload_resumes_remaining_parts`, `test_schemathesis_positive`, `test_schemathesis_negative` (처음 찾은 불일치 M1~M8은 계약·앱을 고치고 봐주는 표 없이 돌린다 — 회귀 시험 `test_fixed_mismatch_*`, ADR-0035)
- [ ] 21. 10GB 끊고 재개 시험 (SeaweedFS) — 요구사항 10.3 (현재는 11MB 축소판)
- [x] 22. 손상 파일 격리 시험 — 요구사항 10.4
- [x] 23. 장비 정책 조회 — 요구사항 11.1 (03번에서 구현)
- [x] 24. 하트비트 저장·`GET /v1/agents/{id}/health` (상태: NEVER_SEEN/OK/STALE/ERROR, 지연은 11번) — 요구사항 11.2, 11.3 · 시험 `test_heartbeat_and_health`
- [x] 25. 자산 목록·상세(감사)·수정(data_role 사유·감사, 위치 정밀도) — contracts assets 태그
- [x] 26. 원래 좌표계 → EPSG:4326 (PostGIS ST_Transform) — 03번 요구사항 6.3 · 시험 `test_source_location_is_transformed_to_4326`
- [x] 27. 파트 길이 서명과 완료 전 크기 검증·정리 — 요구사항 1.1, 1.4, 1.5, 3.1, 4.1, 10.1 · `test_upload_release.py`, SeaweedFS 3.80 서명 검증, Go 빈 PUT 회귀 · ADR-0044
- [x] 28. 재신고 시 기존 만료 업로드 정리·새 세션·C0 자산 ID 유지 — 요구사항 1.5, 2.1, 2.4, 3.2, 3.3 · `test_upload_release.py` · ADR-0045. 체크섬 불일치 격리의 재수집은 운영 정책 결정 필요
- [x] 29. 완료와 취소·만료의 행 잠금 독립 검증 — 요구사항 1.5, 3.3, 3.4, 4.1 · `test_completion_serializes_with_upload_cleanup`의 DELETE·요청 중 만료·주기 정리 3개 경합 · ADR-0044

## 확인 필요 (사람 결정)
- 취소·만료된 C0 시작 세션의 UPLOADING → READY_PRIVATE 역전이를 domain.md 5장 표에 추가할지
- LineageEdge.relation에 SAMPLE_FRAME이 없다. 현장 샘플 프레임은 THUMBNAIL로 기록 중 — 계약에 값을 추가할지
- [x] 23. 업로드 취소·만료 정리가 계보 행 때문에 멈추던 것 수정 — 요구사항 3.3, 3.4(정리·예약 해제), 1.5(예약) · `parent_ref` 업로드의 asset_lineage 행을 먼저 지우고, 만료 정리는 세션마다 SAVEPOINT(한 줄 실패가 모든 기관의 정리를 막지 않음). 시험 `test_upload_release.py`. **남음(검토 2, `docs/review-2026-11-05/`):** 체크섬 격리 재수집 정책(#5)·업로드 중 삭제(#6)·완료·취소 경합(#8)
