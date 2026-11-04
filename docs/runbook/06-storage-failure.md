# 06 스토리지 장애

**판단 기준:** `/readyz`의 storage 실패, 업로드 파트 URL 발급·완료 실패 급증(수집 모니터링 UPLOAD_FAILURE_RATE), 워커 `delete_object_failed`·`quarantine_move_failed` 로그.

**자동 대응:** API는 readiness를 실패로 알리고, 에이전트는 실패한 파트만 백오프로 다시 보낸다(로컬 대기열 유지). 워커 처리 작업은 지수 백오프로 재시도.

**사람이 할 것**
1. `make logs` 또는 `$COMPOSE logs seaweedfs`로 원인(디스크·네트워크·인증) 확인.
2. 스토리지 복구 후 `$COMPOSE restart api worker`.
3. 장애 동안 실패해 FAILED가 된 처리 작업: 운영자 재처리 `POST /v1/assets/{id}/reprocess`(다중 인증).
4. `make verify-integrity`로 표본 객체 해시 확인, 일일 사용량 대조 결과(운영 콘솔) 확인.

**완료 확인:** 업로드 한 건 성공, 수집 모니터링 UPLOAD_FAILURE_RATE·PROCESSING_BACKLOG 경보 해제, 무결성 표본 검증 정상.
