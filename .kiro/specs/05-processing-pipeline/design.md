# 설계: 05 처리 파이프라인

## 작업 큐 (ADR-0014: procrastinate는 psycopg(LGPL) 의존이라 불채택, 자체 큐 보강)
- `jobs`에 asset_id·resource_class·lease_expires_at 추가 (마이그레이션 0005). 가져갈 때 임대 시간을 걸고, 워커 주기 정리가 만료 작업을 QUEUED로 돌린다.
- `job_policies`: 작업 유형별 우선순위·자원 클래스(CPU/GPU)·GPU 없을 때(CPU 실행/WAIT)·최대 시도·임대 시간. 기본 integrity_verify 100 > deid 90 > media_probe 85 > make_proxy 80 > make_thumbnail 75 > tag 60 > embed 50 > reprocess 40.
- 워커는 `WORKER_RESOURCE_CLASSES`(기본 CPU)로 가져갈 작업을 고른다. 실패는 15초×2^(n−1) 백오프(최대 1시간), 최대 시도 후 FAILED + on_exhausted(자산 FAILED, 실패 직전 단계 기록).
- 다른 스펙은 `app.workers.main.register(job_type, fn, on_exhausted)`로 작업 유형을 더한다.

## 흐름
```
REGISTERED (영상, 바이너리 있음, ORIGINAL·CLIP)
 → media_probe: ffprobe → video_ext(해상도·fps·코덱·컨테이너·비트레이트·길이·회전), creation_time 태그 → 촬영시각(DEVICE, 미검증, 근거 없을 때만), ISO 6709 위치
                 읽을 수 없음 → QUARANTINED(CORRUPT_FILE) + 격리 존 이동 + 사용량→격리량
 → make_proxy:   H.264, 짧은 변 480(키우지 않음), 비트레이트 상한, faststart. GPU_ENABLED면 PROXY_HW_ENCODER, 실패 시 CPU로
 → make_thumbnail: 간격마다 고른 프레임을 접촉 인쇄 JPEG 한 장으로 (select 필터)
 → PROXIED → READY_PRIVATE
비영상: REGISTERED → READY_PRIVATE (04번 검증기). C0: 작업 없음.
```
- 파생 자산: 원 자산의 data_role·sensitivity_class·회차·위치 상속, PRIVATE, provenance(PIPELINE, parent, transform, tool_version, job_id), 계보(relation·도구 버전·시각·SERVICE), 무결성 로그(같은 체인), PROCESSED 존 사용량.
- FFmpeg는 별도 프로세스, 인자 목록으로만 호출. 실패 시 stderr 끝 500자를 로그에 남긴다.

## 무결성 로그
- `integrity_anchors`: 날짜별 (마지막 seq, 건수, 마지막 entry_hash)의 해시. 워커 정비 주기(JOB_LEASE_REAP_INTERVAL_S)에 어제까지 생성.
- `python -m app.cli verify-integrity [--sample N]` / `make verify-integrity`: 체인 재계산 + 표본 객체 SHA-256 재계산 → 단절·불일치·없는 객체 보고.

## API
`POST /v1/assets/{id}/reprocess` (PLATFORM_OPERATOR, MFA, 사유 필수, 감사): META·PROXY·THUMBNAIL 중 고른 단계를 순서대로. FAILED 자산은 실패 직전 단계로 되돌린 뒤.

## 추론 (요구사항 8)
`app/adapters/onnx_inference.py`: Model_Registry(`model_registry` 표: 이름·버전·sha256·라이선스·가중치 위치·학습 데이터 조건)에 있고, 라이선스가 허용 목록이며, 가중치 sha256이 일치해야 로드. onnxruntime은 선택 설치, GPU_ENABLED면 CUDA 공급자. 탐지 처리는 14번.

## 측정
- 워커 메트릭: 작업 유형별 처리 시간·완료·실패·대기 수, `worker_frames_processed_total`·`worker_frame_processing_seconds_total`(초당 처리 프레임).
- `make bench`: 합성 영상으로 META·PROXY_CPU·THUMBNAIL(+GPU면 PROXY_GPU) 시간·fps·실시간 배수를 `var/bench.json`으로. 이 클라우드 환경(4 vCPU) 1280x720 10초: 프록시 6.8배속, 썸네일 16배속.

## 확인 필요 (사람 결정)
- 파이프라인이 파생 자산을 만드는 생성 행 ((없음) → READY_PRIVATE, 주체 SERVICE)이 domain.md 5장 표에 없다. 지금은 생성 시 바로 READY_PRIVATE로 둔다.
- 개발 이미지의 FFmpeg는 Debian 패키지(7.1, GPL 구성 포함)다. 납품 이미지의 빌드 구성·고지는 17번에서 기록한다.
