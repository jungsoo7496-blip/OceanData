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
- [x] 13. 대용량 영상 처리 임시 디스크 한도·정리 — `WORK_DIR`, 받기 전에 여유 확인(크기 × `WORK_SIZE_FACTOR`(2.5) + `WORK_MIN_FREE_BYTES`(2GB)) → 모자라면 `WorkSpaceShortage`로 작업 큐 백오프 재시도, 비정상 종료로 남은 `marine-*` 임시 파일은 `WORK_STALE_AFTER_S`(6시간) 뒤 워커 주기 정리 · 시험 `tests/unit/test_work_space.py`. 실제 대용량 실측은 하드웨어 후
- [x] 14. Model_Registry 등록 API·화면 — 요구사항 8.3·8.4 · `GET/POST /v1/models`(운영자·다중 인증, 허용 라이선스만·버전 불변·학습 데이터 조건 필수, 조회는 REVIEWER도), 운영 콘솔 '모델 등록부' · 시험 `tests/integration/test_model_registry.py`(등록·로드 판단 sha256). 실제 모델은 14번 선정 후
- [x] 15. 클립 작업이 워커를 오래 쥐지 않게 — 요구사항 1.2(한 작업을 두 워커에 배정하지 않음), 1.3(임대), 09번 7.2(구간 클립) · 클립 ffmpeg·ffprobe 한도를 구간 길이 × `CLIP_TIMEOUT_FACTOR`(4, 최소 `CLIP_TIMEOUT_MIN_S` 120초, 최대 `FFMPEG_TIMEOUT_S`)로 함. ffmpeg가 넘으면 끝내고 재시도 없이 FAILED(`CLIP_TIMEOUT`, 계약 설명·화면 문구), ffprobe만 넘으면 길이 정보 없이 저장. 워커가 가져갈 작업 유형 `WORKER_JOB_TYPES`·`WORKER_EXCLUDE_JOB_TYPES`(기본 모든 유형, 오타·빈 결과면 컨테이너를 만들기 전에 기동 실패, Compose 선택 서비스 `worker-clips`·프로필 clip-worker, runbook 07). `register(..., renew_lease=True)` 유형(clip_export)은 도는 동안 임대의 1/3마다 `QueuePort.renew_lease`로 다시 걸어, 임대보다 오래 걸려도 다른 워커가 회수해 두 번 돌리지 않음(검토 2026-11-12 C7). 배수는 개발 PC 실측(합성 4K·스레드 4개 구간 1초당 0.86초)의 약 4.6배. 우선순위는 맞아 그대로. 시험 `test_clip_job_bounds.py`(7개, 수정 전 6개 실패). ADR-0069
- [x] 16. 검증에서 실패한 자산의 재처리 — 요구사항 7.2, 7.1 · 무결성 검증이 재시도를 다 써 FAILED(failed_from=VALIDATING)가 된 자산을 재처리하면 VALIDATING으로만 돌리고 처리 단계만 넣어 영구히 남던 것(계약 점검 S22). 검증 작업을 이번 실행의 키로 다시 넣고 등록 뒤 보통 처리 순서. 재처리는 자산 행을 잠그고 최신 상태로 판단(겹친 요청이 등록된 자산을 덮어쓰지 않음) · 시험 test_verify_resume.py::test_reprocess_after_exhausted_verification_restarts_verification, ::test_overlapping_reprocess_decides_on_the_locked_current_state · ADR-0076
