# 요구사항: 04 수집 API (업로드·검증·격리·웹훅)

## 소개
에이전트·웹·기관 서버가 공통으로 쓰는 업로드 API를 만든다.
메타데이터는 API로, 대용량 바이너리는 사전 서명 URL로 스토리지에 직접 보낸다.
받은 파일은 서버가 해시를 다시 계산해 검증하고, 실패하면 격리한다.

## 범위 밖
- 에이전트 클라이언트 (06번), 프록시 등 후처리 (05번), 쿼터 알림·대조 (08번), 쿼터 정책 관리 화면 (09번)

## 용어집
- **Ingestion_API**: 업로드 관련 HTTP API
- **Upload**: 업로드 세션 (`upl_`)
- **Manifest**: 파일 메타데이터 신고서 (장비, 회차 또는 회차 규칙, 크기, sha256, 신고 MIME, 촬영시각 근거, data_role). 형식은 `contracts/schemas/upload-manifest.schema.json`이며 organization_id는 넣지 않는다 (인증 정보로 정한다)
- **Part_URL**: 멀티파트 파트별 사전 서명 업로드 URL
- **Integrity_Verifier**: 저장된 객체의 해시를 다시 계산하는 워커 작업
- **Receipt**: 등록 완료 영수증
- **Webhook_Dispatcher**: 이벤트를 구독자에게 서명해 보내는 구성요소
- **Rate_Limiter**: 클라이언트별 호출 제한
- **Quota_Manager**: 기관 사용량·예약 관리 (예약·확정·해제는 이 스펙, 알림·대조·리포트는 08번)
- **Integrity_Log**: 등록 해시 체인 (항목 추가는 이 스펙, 일일 앵커·검증 명령은 05번)

## 요구사항

### 요구사항 1: 업로드 생성
**사용자 스토리:** 에이전트로서, 큰 영상 파일을 안정적으로 올릴 준비를 하고 싶다.

#### 수용 기준
1. WHEN `POST /v1/uploads`로 Manifest를 받으면, THE Ingestion_API SHALL 자격증명 범위(수집기와 매핑된 원천 장비)·원천 장비 정책·variant 허용 여부(domain.md 3장)·쿼터를 검사한 뒤 upload_id, 파트 크기, 파트 수, 파트별 Part_URL(기본 유효 15분)을 반환한다.
2. THE Ingestion_API SHALL 바이너리를 API 서버로 받지 않는다.
3. IF 장비의 collection_mode가 C0_METADATA_ONLY이면, THEN THE Ingestion_API SHALL Part_URL을 발급하지 않고 메타데이터만 자산으로 등록한다 (현장 파일의 크기·sha256은 기록한다).
4. IF 기관 사용량 + 진행 중 예약량 + 신고 크기가 쿼터를 넘으면, THEN THE Ingestion_API SHALL 409 QUOTA_EXCEEDED를 반환하고 URL을 발급하지 않는다.
5. WHEN 업로드를 생성하면, THE Quota_Manager SHALL 신고 크기만큼 쿼터를 예약하고, 등록되면 확정하고, 격리되면 격리 사용량으로 옮기고, 실패·만료·취소되면 해제한다. 동시에 여러 업로드가 생성되어도 예약 합계가 쿼터를 넘지 않는다.
6. IF 스토리지 상태 확인에 실패하면, THEN THE Ingestion_API SHALL 503 STORAGE_UNAVAILABLE과 Retry-After를 반환한다.
7. IF variant가 원천 장비의 collection_mode에서 허용되지 않으면, THEN THE Ingestion_API SHALL 422 RULE_VIOLATION과 허용 variant 목록을 반환한다.

### 요구사항 2: 멱등성
**사용자 스토리:** 에이전트로서, 재시도해도 중복이 생기지 않길 원한다.

#### 수용 기준
1. IF 같은 (기관, 장비, sha256)의 자산에 바이너리가 이미 저장되어 있으면, THEN THE Ingestion_API SHALL URL을 발급하지 않고 기존 asset_id와 상태 ALREADY_REGISTERED를 반환한다.
2. WHEN 같은 Idempotency-Key로 같은 본문의 요청이 다시 오면, THE Ingestion_API SHALL 처음과 같은 응답을 반환한다.
3. IF 같은 Idempotency-Key로 본문이 다른 요청이 오면, THEN THE Ingestion_API SHALL 409 IDEMPOTENCY_CONFLICT를 반환하고 새 세션을 만들지 않는다.
4. WHEN 같은 키의 자산이 바이너리 없는 C0 자산이고 원천 장비 정책이 그 variant를 허용하면, THE Ingestion_API SHALL 그 asset_id로 업로드 세션을 연다.

### 요구사항 3: 재개
**사용자 스토리:** 선박 담당자로서, 통신이 끊겼다 이어져도 처음부터 다시 올리지 않길 원한다.

#### 수용 기준
1. WHEN `GET /v1/uploads/{id}/parts`를 받으면, THE Ingestion_API SHALL 완료된 파트 목록과 남은 파트의 새 Part_URL을 반환한다.
2. WHILE 업로드 세션이 유효 기간(기본 7일, 설정값) 안에 있으면, THE Ingestion_API SHALL 재개를 허용한다.
3. IF 유효 기간이 지나면, THEN THE Ingestion_API SHALL 410 GONE을 반환하고 스토리지의 미완료 멀티파트를 정리하고 쿼터 예약을 해제한다.
4. WHEN `DELETE /v1/uploads/{id}`를 받으면, THE Ingestion_API SHALL 미완료 멀티파트를 정리하고 쿼터 예약을 해제한다.

### 요구사항 4: 완료와 무결성 검증
**사용자 스토리:** 제공기업으로서, 올린 파일이 손상 없이 저장됐다는 증거를 받고 싶다.

#### 수용 기준
1. WHEN `POST /v1/uploads/{id}/complete`에 파트 목록을 받으면, THE Ingestion_API SHALL 멀티파트를 완료하고 ingest_status를 RECEIVED로 바꾸고 무결성 검증 작업을 등록한 뒤 202를 반환한다.
2. THE Integrity_Verifier SHALL 저장된 객체를 스트리밍으로 읽어 SHA-256을 다시 계산한다.
3. IF 해시가 다르면, THEN THE Integrity_Verifier SHALL 자산을 QUARANTINED(CHECKSUM_MISMATCH)로 바꾸고 업로더에게 알린다.
4. WHEN 해시가 같으면, THE Integrity_Verifier SHALL 내용(매직 바이트)으로 형식을 판별하고 필수 메타데이터를 검사한 뒤 REGISTERED로 바꾼다.
5. IF 판별한 형식이 신고 MIME과 다르면, THEN THE Integrity_Verifier SHALL QUARANTINED(MIME_MISMATCH)로 바꾼다.
6. WHEN 자산이 REGISTERED가 되면, THE Integrity_Verifier SHALL Integrity_Log에 (asset_id, sha256, registered_at, prev_hash, entry_hash) 항목을 추가한다.
7. WHERE 기관 정책이 악성코드 검사를 요구하면, THE Integrity_Verifier SHALL 외부 검사기 어댑터로 검사하고 의심되면 QUARANTINED(MALWARE_SUSPECTED)로 바꾼다 (검사기 제품은 결정 대기).

### 요구사항 5: 격리
**사용자 스토리:** 제공기업 담당자로서, 왜 내 파일이 등록되지 않았는지 알고 다시 보내고 싶다.

#### 수용 기준
1. THE 시스템 SHALL 격리 사유를 CHECKSUM_MISMATCH, MISSING_REQUIRED_METADATA, UNKNOWN_SESSION, UNSUPPORTED_FORMAT, CORRUPT_FILE, MIME_MISMATCH, MALWARE_SUSPECTED 코드로 기록한다.
2. WHEN 자산이 격리되면, THE 시스템 SHALL 객체를 QUARANTINE 존으로 옮기고 사유별 격리 보존 규칙(domain.md 9장 — UNKNOWN_SESSION·MISSING_REQUIRED_METADATA는 자동 삭제 없음)을 적용한다.
3. WHEN 회차 미상(UNKNOWN_SESSION)으로 격리된 자산에 사용자가 회차를 지정하면, THE 시스템 SHALL 다시 검증해 등록한다.
4. THE 시스템 SHALL 격리 데이터를 삭제하지 않고 사유와 함께 보관한다 (보존 기간이 끝날 때까지).

### 요구사항 6: 회차 결정
**사용자 스토리:** 제공기업 담당자로서, 폴더에 넣기만 하면 실증회차가 자동으로 정해지길 원한다.

#### 수용 기준
1. WHEN Manifest에 session_id가 없고 장비에 회차 추출 규칙이 있으면, THE Ingestion_API SHALL 규칙으로 회차를 정한다.
2. IF 회차를 정할 수 없으면, THEN THE 시스템 SHALL 자산을 UNKNOWN_SESSION으로 격리하고 해당 기관 담당자에게 분류를 요청한다.

### 요구사항 7: 영수증과 상태
**사용자 스토리:** 제공기업으로서, 등록 증명을 보관하고 싶다.

#### 수용 기준
1. WHEN 자산이 REGISTERED가 되면, THE 시스템 SHALL asset_id, sha256, byte_size, registered_at, 무결성 로그 항목 ID를 담은 Receipt를 만든다.
2. WHEN `GET /v1/uploads/{id}`를 받으면, THE Ingestion_API SHALL ingest_status, 파트 진행률, 오류 코드를 반환하며 RECEIVED와 REGISTERED를 구분한다.
3. WHEN `GET /v1/assets/{id}/receipt`를 받으면, THE 시스템 SHALL Receipt를 반환한다.

### 요구사항 8: 웹훅
**사용자 스토리:** 자체 서버가 있는 기관으로서, 처리 결과를 폴링 없이 받고 싶다.

#### 수용 기준
1. THE Webhook_Dispatcher SHALL `/v1/webhooks/subscriptions`로 구독(이벤트 종류, URL, 서명 비밀)을 등록·조회·삭제할 수 있게 한다.
2. WHEN upload.registered, upload.quarantined, asset.processed, share.status_changed, grant.revoked 이벤트가 생기면, THE Webhook_Dispatcher SHALL HMAC-SHA256 서명과 이벤트 ID를 붙여 전송한다.
3. IF 전송이 실패하면, THEN THE Webhook_Dispatcher SHALL 지수 백오프로 최대 N회(설정값) 재시도하고, 계속 실패하면 구독을 비활성화하고 기관에 알린다.

### 요구사항 9: 호출 제한
**사용자 스토리:** 운영자로서, 한 클라이언트가 서비스를 독점하지 못하게 하고 싶다.

#### 수용 기준
1. THE Rate_Limiter SHALL 클라이언트별 분당 요청 수를 설정값으로 제한한다.
2. IF 제한을 넘으면, THEN THE Ingestion_API SHALL 429 RATE_LIMITED와 Retry-After 헤더를 반환한다.

### 요구사항 10: 계약 문서와 시험
**사용자 스토리:** 외부 개발자로서, 문서만 보고 연동하고 싶다.

#### 수용 기준
1. THE Ingestion_API SHALL `contracts/openapi.yaml`에 정의된 업로드 엔드포인트·오류 코드·예제와 일치하게 동작한다. 계약을 바꿔야 하면 계약부터 고친다.
2. THE 저장소 SHALL Python 예제 클라이언트로 업로드·재개·완료 계약 테스트를 제공하고, `contracts/openapi.yaml` 기준 자동 계약 시험(Schemathesis 등)을 CI에서 돌린다.
3. THE 통합 테스트 SHALL 10GB 이상 파일을 전송하다 연결을 끊고 재개해 완료되는지 SeaweedFS로 확인한다 (T03).
4. THE 통합 테스트 SHALL 의도적으로 손상시킨 파일이 격리되는지 확인한다 (T04).

### 요구사항 11: 에이전트 지원 API
**사용자 스토리:** 에이전트 개발자로서, 정책을 받고 상태를 보고할 서버 쪽 API가 먼저 있어야 한다.

#### 수용 기준
1. WHEN 수집기가 `GET /v1/devices/{id}/policy`를 요청하면, THE Ingestion_API SHALL `contracts/schemas/device-policy.schema.json` 형식으로 수집기 정책과 매핑된 원천 장비 정책을 반환한다.
2. WHEN `POST /v1/agents/heartbeat`를 받으면, THE Ingestion_API SHALL 수집기 범위를 확인하고 `contracts/schemas/agent-heartbeat.schema.json` 형식의 상태를 저장한다.
3. WHEN `GET /v1/agents/{id}/health`를 받으면, THE Ingestion_API SHALL 마지막 하트비트·대기량·최근 오류·적용 정책 버전을 반환한다 (지연 판정과 경보는 11번).

## 추적
- 기능 ID: ING-02, ING-03, ING-04, ING-05(서버 측), API-01(호출 제한), API-02, API-03
- 수용시험: T01(T01-e), T02(T02-c, T02-d, T02-h), T03(T03-a, T03-c), T04(T04-a, T04-b, T04-c, T04-d, T04-e, T04-g), T05(T05-e), T12(T12-a), T13(T13-a), T15(T15-f) — 세부는 docs/ACCEPTANCE.md
- 하드웨어 의존: 없음 (S3 API만 사용)
