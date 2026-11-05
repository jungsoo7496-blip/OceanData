# 설계: 04 수집 API

## 흐름
```
POST /v1/uploads (매니페스트)
  → 검증(contracts.validate_document: schema_version 먼저 → 스키마 → 위치·시간 도메인 규칙)
  → 원천 장비 범위(DEVICE는 매핑된 원천만) → 사업·회차 결정 → 멱등(기관·장비·sha256)
  → C0이면 메타데이터 전용 등록 / 아니면 variant 허용 검사 → 쿼터 예약(행 잠금) → 멀티파트 생성 → 파트 URL
클라이언트 → 사전 서명 URL로 파트 PUT (API를 거치지 않음)
POST /v1/uploads/{id}/complete → 멀티파트 완료 → RECEIVED → integrity_verify 작업 등록 (202)
워커 integrity_verify → VALIDATING → 스트리밍 sha256·크기 → 매직 바이트 형식·MIME → 회차
  → REGISTERED(+무결성 로그·영수증, 쿼터 확정) → 비영상은 READY_PRIVATE, 영상은 make_proxy 작업(05번)
  → 실패 시 QUARANTINED(사유) + 격리 존 이동 + 업로더 통지 + 격리 사용량
PATCH /v1/assets/{id} {session_id} → UNKNOWN_SESSION 격리 자산 재검증 (QUARANTINED → VALIDATING) → 정식 위치로 이동
```

## 데이터 (마이그레이션 0003)
- `uploads`: 세션(S3 upload id, 파트 크기·수, 신고 크기, 예약량, 상태 UPLOADING/RECEIVED/ABORTED/EXPIRED, 만료). asset_id는 ON DELETE SET NULL
- `org_quotas`: quota·used·quarantine·reserved·backup (domain.md 9장 계산식). 예약은 `SELECT … FOR UPDATE`로 직렬화
- `integrity_log`: (asset_id, sha256, byte_size, registered_at, prev_hash, entry_hash) 추가 전용 해시 체인 (트리거·권한)
- `assets.ingest_context`: 감시 규칙·로컬 ID·좌표 변환 이력·업로더 (API로 노출 안 함)

## 결정
| 항목 | 결정 |
|---|---|
| 객체 키 | domain.md 9장 그대로. 원래 파일명은 provenance에만 |
| 존 | ORIGINAL → RAW, 그 외 variant → PROCESSED, 격리 → QUARANTINE |
| 격리 이동 | CopyObject는 HW-1 공통 기능 목록 밖이라 GET 스트림 → upload_fileobj(멀티파트) → DELETE |
| 사전 서명 주소 | `S3_PUBLIC_ENDPOINT_URL`로 서명 (컨테이너 내부 주소와 분리) |
| 파트 크기 | `UPLOAD_PART_SIZE`(기본 64MiB), 파트 10,000개를 넘지 않게 자동 확대 |
| 회차 추출 규칙 | session_id → session_label(기관 안 유일) → 장비가 연결된 회차 중 촬영시각을 포함하는 회차가 정확히 하나 |
| C0 + 회차 없음 | 등록 상태가 될 수 없어 422 RULE_VIOLATION (바이너리가 없어 거부해도 손실 없음) |
| 취소·만료 | 멀티파트 정리, 예약 해제. 받은 것 없는 새 자산 기록은 삭제(같은 파일 재전송 가능). C0에서 시작한 세션은 READY_PRIVATE로 되돌림 — **domain.md 5장 표에 없는 역전이라 승인 필요** |
| 형식 판별 | 매직 바이트(mp4/mov ftyp, mkv/webm, jpeg, png, pdf, zip, riff) + UTF-8 텍스트(csv/json) |
| 호출 제한 | 주체별 분당 `RATE_LIMIT_PER_MINUTE` (프로세스 메모리). 다중 인스턴스는 게이트웨이에서 |

## 시험
`backend/tests/integration/test_ingest.py` — moto S3 서버 또는 `TEST_S3_ENDPOINT`(SeaweedFS)로 실제 사전 서명 업로드. 두 환경 모두 통과 확인 (2026-11-04).

## 웹훅 (요구사항 8, ADR-0030)
- 구독 `GET/POST /v1/webhooks/subscriptions`, `DELETE …/{id}`(ORG_ADMIN 또는 webhook:manage API 키, 자기 기관). 비밀은 등록 응답에서 한 번,
  저장은 salt만(서버 키로 다시 만듦). https만, 사설·루프백 주소 거부.
- 발행: `services/webhooks.py`의 `before_flush` outbox → `webhook_deliveries`(0015). 전송: 워커 `WEBHOOK_DISPATCH_INTERVAL_S`마다
  `deliver_due()`(SKIP LOCKED, HMAC-SHA256 `timestamp.body`, X-Event-Id, 리디렉션 안 따름), 지수 백오프, `WEBHOOK_MAX_ATTEMPTS`번 실패 시 구독 끔 +
  남은 전송 취소 + 기관 ORG_ADMIN 메일.

