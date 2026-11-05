# 작업: 05 처리 파이프라인

- [x] 1. procrastinate PoC → 불채택 ADR-0014 (psycopg LGPL), 자체 큐 보강 — 요구사항 1.1, 1.2
- [x] 2. 임대 만료 작업 회수 — 요구사항 1.3 · 시험 `test_queue_retry_lease_and_resource_classes`
- [x] 3. 영상 메타→프록시→썸네일 순서 등록, PROXIED→READY_PRIVATE, 비영상·C0 건너뜀, 작업 유형 등록 API — 요구사항 2.1~2.5 · 시험 `test_video_pipeline_to_ready_private`
- [x] 4. ffprobe 메타 추출·생성 시각 태그(DEVICE, 미검증)·GPS — 요구사항 3.1, 3.2 · 시험 `test_parse_probe`
- [x] 5. 손상 파일 CORRUPT_FILE 격리 — 요구사항 3.3 · 시험 `test_corrupt_video_is_quarantined`
- [x] 6. 프록시(짧은 변 480, 상한, faststart, HW 인코더→CPU 대체)·썸네일·계보 — 요구사항 4.1~4.4 (HW 인코더는 GPU 장비에서 확인 필요)
- [x] 7. 파생 자산도 같은 무결성 체인, 일 단위 앵커, 검증 명령(체인+표본 해시) — 요구사항 5.1~5.3 · 시험 `test_integrity_anchor_and_tamper_detection`
- [x] 8. 정책 표 우선순위·GPU 클래스·CPU 대체/대기 — 요구사항 6.1, 6.2 · 시험 `test_queue_retry_lease_and_resource_classes`
- [x] 9. 업로드 API와 처리 분리 (워커 비동기) — 요구사항 6.3
- [x] 10. 지수 백오프 재시도·FAILED·재처리 API — 요구사항 7.1, 7.2 · 시험 `test_reprocess_api`
- [x] 11. Model_Registry·라이선스·sha256 확인 후 로드, CPU 기본·CUDA — 요구사항 8.1~8.4 · 시험 `test_model_registry_checks` (실제 onnxruntime 로드는 14번에서)
- [x] 12. 워커 메트릭(처리 시간·프레임·대기·실패)·`make bench` — 요구사항 9.1, 9.2
- [ ] 13. 대용량(수 GB) 영상 처리 시 임시 디스크 사용량 한도·정리 정책 (지금은 작업마다 임시 파일 후 삭제)
- [x] 14. Model_Registry 등록 API·화면 — 요구사항 8.3·8.4 · `GET/POST /v1/models`(운영자·다중 인증, 허용 라이선스만·버전 불변·학습 데이터 조건 필수, 조회는 REVIEWER도), 운영 콘솔 '모델 등록부' · 시험 `tests/integration/test_model_registry.py`(등록·로드 판단 sha256). 실제 모델은 14번 선정 후
