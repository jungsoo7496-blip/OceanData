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
| 선언 크기 강제 | 계획한 길이로 파트의 Content-Length 서명, 완료 전 ListParts 크기 재검사. 불일치가 확인되어 저장소를 정리하면 409와 세션 취소, 크기 확인·정리 실패는 예약을 유지하고 503으로 재시도(ADR-0044) |
| 회차 추출 규칙 | session_id → session_label(기관 안 유일) → 장비가 연결된 회차 중 촬영시각을 포함하는 회차가 정확히 하나 |
| C0 + 회차 없음 | 등록 상태가 될 수 없어 422 RULE_VIOLATION (바이너리가 없어 거부해도 손실 없음) |
| 취소·만료 | 멀티파트 정리, 예약 해제. 받은 것 없는 새 자산 기록은 삭제(같은 파일 재전송 가능). C0에서 시작한 세션은 READY_PRIVATE로 되돌림 — **domain.md 5장 표에 없는 역전이라 승인 필요** |
| 형식 판별 | 매직 바이트(mp4/mov ftyp, mkv/webm, jpeg, png, pdf, zip, riff) + UTF-8 텍스트(csv/json) |
| 호출 제한 | 주체별 분당 `RATE_LIMIT_PER_MINUTE` (프로세스 메모리). 다중 인스턴스는 게이트웨이에서 |

## 시험
`backend/tests/integration/test_ingest.py` — moto S3 서버 또는 `TEST_S3_ENDPOINT`(SeaweedFS)로 실제 사전 서명 업로드. 두 환경 모두 통과 확인 (2026-11-04).

## 웹훅 (요구사항 8, ADR-0030·0041)
- 구독 `GET/POST /v1/webhooks/subscriptions`, `DELETE …/{id}`(ORG_ADMIN 또는 webhook:manage API 키, 자기 기관). 비밀은 등록 응답에서 한 번,
  저장은 salt만(서버 키로 다시 만듦). https만, 공인(global)이 아닌 주소(사설·루프백·공유 100.64/10 등) 거부. 기관당 구독은
  `WEBHOOK_MAX_SUBSCRIPTIONS_PER_ORG`(기본 10, 중지 포함)개까지 — 넘으면 409 CONFLICT. 다른 기관 구독 삭제는 Tenant_Guard(`load_scoped`)로
  404 + `CROSS_ORG_ACCESS_ATTEMPT` 감사.
- 발행: `services/webhooks.py`의 `before_flush` outbox → `webhook_deliveries`(0015).
- 전송 (ADR-0041): 워커의 전용 스레드(`start_webhook_dispatcher`)가 `deliver_due()` 라운드를 돈다 — 작업 루프와 떨어져 있어 느린 수신
  서버가 작업 처리를 멈추지 않는다. 라운드마다 ① 가져오기: 구독당 1건, 기관당 `WEBHOOK_MAX_PER_ORG`건, 모두 `WEBHOOK_BATCH_SIZE`건을
  구독·전송 행 모두 SKIP LOCKED로 골라 `SENDING`(임대 = `next_attempt_at`)으로 커밋 ② 트랜잭션 밖에서 동시에 보내기: 한 건 전체
  (이름 풀기~응답 헤더) `WEBHOOK_TIMEOUT_S`, 상태 코드만(본문 안 읽음), 검사한 주소로 고정 연결(이름은 Host·SNI·인증서 확인에만),
  프록시 환경변수·리디렉션 안 따름, HMAC-SHA256 `timestamp.body`·X-Event-Id ③ 건마다 짧은 트랜잭션으로 기록: 지수 백오프,
  `WEBHOOK_MAX_ATTEMPTS`번 실패 시 구독 끔 + 남은 전송 취소 + (커밋 뒤) 기관 ORG_ADMIN 메일. 임대가 끝난 `SENDING`은 다시 보낸다
  (최소 한 번). 거부 이유(이름 못 풂·공인 아닌 주소)는 기관에 같은 문구, 자세한 건 서버 로그.
- 시험: `tests/integration/test_webhooks.py`(두 워커 동시·보내는 동안 행 잠금 없음·실제 전송 기한·임대·경합 재확인·공정성·구독 상한·
  교차 기관 감사·전용 스레드), `tests/unit/test_webhook_send.py`(헤더·본문 흘리기, 이름 풀기 기한, 주소 고정·프록시 무시, TLS SNI·인증서,
  공인 주소 판정).

## 악성코드 검사 (요구사항 4.7, ADR-0031)
- `ports/malware.py` 스트리밍 포트, `adapters/malware_scanners.py`(`MALWARE_SCANNER=none | mock-eicar`). 기관 정책 `organizations.malware_scan_required`(0016),
  운영자 명령 `org-policy`. 무결성 검증이 해시를 계산하며 같은 조각을 검사기에 넘기고, 의심이면 QUARANTINED(MALWARE_SUSPECTED).
  정책은 켜졌는데 검사기가 없으면 상태 전이 전에 멈춰 재시도한다. 실제 검사기 제품은 결정 대기.

