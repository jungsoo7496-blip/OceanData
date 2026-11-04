# 요구사항: 03 데이터 모델·메타데이터 표준

## 소개
기관·실증사업·실증회차·장비·자산의 데이터 모델, 공통 메타데이터 v1.0 스키마, 검증·정규화, 코드표, 품질 평가, 계보를 만든다.
값과 규칙은 domain.md를 그대로 따른다.

## 범위 밖
- 업로드 흐름 (04번), 처리 작업 (05번), 공유 규칙 실행 (13번)

## 용어집
- **Catalog_Store**: 엔터티를 저장하는 DB 모델·저장소
- **Schema_Registry**: `contracts/schemas`의 JSON Schema와 이를 제공하는 API
- **Contract_Validator**: 저장 없이 매니페스트·관측값·이벤트를 계약으로 검사하는 API
- **Integration_Card**: 데이터 연계 카드 기록 (기준 문서 부록 A)
- **Metadata_Validator**: 메타데이터 필수·형식 검사기
- **Normalizer**: 시간·위치·단위 정규화기
- **Code_Table**: 버전 관리되는 코드표
- **Quality_Evaluator**: 품질 규칙 평가기
- **Lineage**: 원본과 파생 자산의 관계 기록
- **Observation_Store**: 센서·관측 시계열 저장

## 요구사항

### 요구사항 1: 엔터티 모델
**사용자 스토리:** 개발자로서, 모든 데이터가 같은 계층 구조로 들어오길 원한다.

#### 수용 기준
1. THE Catalog_Store SHALL 기관·실증사업·실증회차·장비·자산·데이터셋·데이터셋 버전을 domain.md 1장의 계층과 ID 접두사로 저장한다.
2. THE Catalog_Store SHALL 실증회차를 실증사업에, 자산을 기관·실증사업·장비에 연결한다. 실증회차가 정해지지 않은 자산은 QUARANTINED(UNKNOWN_SESSION)로 둔다 (domain.md 5장).
3. THE Catalog_Store SHALL 장비를 원천 장비와 수집기로 구분하고(domain.md 1장), 수집기-원천 장비 매핑과 원천 장비별 정책(collection_mode, 허용 data_role, 감시 규칙별 data_role 기본값, 전송 허용 시간대, 대역폭 한도, local_copy_policy와 현장 보존 일수, 기대 수집 주기, source_timezone, Observation_Mapping 버전(07번))을 버전과 함께 저장한다.
4. THE Catalog_Store SHALL 기관별 약정(1단·2단 구분, 약정 참조, 유효기간, 2단 허용 목적 코드)을 저장한다. 공유 규칙(13번)은 이 기록으로 약정 유효 여부를 판단한다.
5. THE Catalog_Store SHALL Integration_Card를 기관·장비 단위로 버전과 함께 저장하고, 카드에서 만든 장비 정책과 연결한다 (화면은 09번).

### 요구사항 2: 스키마 게시
**사용자 스토리:** 외부 개발자로서, 메타데이터 규격을 기계가 읽을 수 있는 형태로 받고 싶다.

#### 수용 기준
1. THE Schema_Registry SHALL 공통 메타데이터 v1.0과 유형별 확장(video_ext, cctv_ext, vessel_ext, sensor_ext, external_obs_ext, annotation_ext, evidence_ext)의 JSON Schema를 `contracts/schemas`에서 읽는다. 이 패키지에 초안이 들어 있다.
2. WHEN `GET /v1/schemas` 또는 `GET /v1/schemas/{name}/{version}`을 받으면, THE Schema_Registry SHALL `contracts/schemas`의 해당 스키마를 참조가 풀린 단일 문서(bundle)로 반환한다. 내용이 저장소 파일과 같은지 CI에서 비교한다.
3. THE backend와 web SHALL 이 스키마를 기준으로 타입을 생성하거나 검증한다.

### 요구사항 3: 기본값
**사용자 스토리:** 제공기업 담당자로서, 아무것도 입력하지 않아도 안전한 상태로 저장되길 원한다.

#### 수용 기준
1. WHEN 자산이 생성되면, THE Catalog_Store SHALL domain.md 3장의 기본값을 적용한다: collection_mode는 장비 정책에서 상속, data_role은 장비·폴더 기본값이 없으면 CORE, privacy_status는 REVIEW_REQUIRED, sensitivity_class는 RESTRICTED, sharing_scope는 PRIVATE.
2. THE 시스템 SHALL 기업 사용자에게 필수 메타데이터 직접 입력을 요구하지 않는다.

### 요구사항 4: 검증
**사용자 스토리:** 운영자로서, 잘못된 메타데이터가 섞이지 않길 원한다.

#### 수용 기준
1. WHEN 메타데이터가 제출되면, THE Metadata_Validator SHALL data_type별 필수 필드와 형식을 검사한다.
2. IF 검사에 실패하면, THEN THE Metadata_Validator SHALL VALIDATION_ERROR와 필드별 사유를 반환한다.
3. THE Metadata_Validator SHALL 스키마 검증 전에 schema_version을 확인한다. 주버전이 다르거나 서버가 모르는 더 높은 부버전이면 422 UNSUPPORTED_SCHEMA_VERSION을 반환하고, 같거나 낮은 부버전은 받아들인다.

### 요구사항 5: 촬영시각 규칙
**사용자 스토리:** AI 이용기업으로서, 촬영시각이 정확한지 믿을 수 있어야 한다.

#### 수용 기준
1. THE Normalizer SHALL 파일 생성·수정 시각과 업로드 시각을 촬영시각으로 사용하지 않는다.
2. IF 촬영시각 근거(영상 내부 메타, 장비 로그, GPS, 장비 매니페스트)가 없으면, THEN THE Normalizer SHALL capture_start_at·capture_end_at을 비우고 timestamp_quality.source=UNKNOWN, quality_status=WARNING으로 기록한다.
3. WHEN 시각에 시간대 오프셋이 있으면, THE Normalizer SHALL UTC로 변환하고 원래 값을 provenance에 보존한다.
4. THE Normalizer SHALL timestamp_quality에 근거 출처·검증 여부·추정 오차를 기록한다.

### 요구사항 6: 위치 규칙
**사용자 스토리:** 개발자로서, 위경도가 뒤바뀐 데이터를 막고 싶다.

#### 수용 기준
1. WHEN location이 제출되면, THE Normalizer SHALL GeoJSON 형식과 좌표 범위(경도 −180~180, 위도 −90~90)를 검사한다.
2. IF 위도 자리의 값이 범위를 벗어나 좌표 순서가 뒤바뀐 것으로 보이면, THEN THE Normalizer SHALL 거부하고 사유를 반환한다.
3. WHERE 원래 좌표계가 지정되면, THE Normalizer SHALL EPSG:4326으로 변환하고 원래 좌표계와 변환 이력을 보관한다.
4. THE Catalog_Store SHALL location_precision에 따라 응답의 위치 정밀도를 낮춰 보여줄 수 있게 한다.

### 요구사항 7: 단위 규칙
**사용자 스토리:** 데이터 이용자로서, 기관마다 다른 단위가 통일되길 원한다.

#### 수용 기준
1. WHEN 센서 값이 제출되면, THE Normalizer SHALL 단위 사전으로 정규화하고 원래 값과 원래 단위를 보존한다.
2. IF 단위 사전에 없는 단위이면, THEN THE Normalizer SHALL 원래 값을 그대로 저장하고, 관측값은 quality_flag=SUSPECT, 파일 자산은 quality_status=WARNING으로 기록한다.

### 요구사항 8: 코드표
**사용자 스토리:** 운영자로서, 이벤트 코드를 공통으로 쓰되 기관별 세부 코드도 허용하고 싶다.

#### 수용 기준
1. THE Code_Table SHALL data_type, 이벤트 코드(공통 상위분류 + 기관별 하위코드), 단위, 보존 정책 코드, 이용 목적 코드, 격리 사유 코드를 버전과 함께 관리하며 초기값은 `contracts/codes/`에서 읽는다.
2. WHEN `GET /v1/codes/{table}`을 받으면, THE 시스템 SHALL 현재 버전의 코드 목록을 반환한다.
3. THE 시스템 SHALL ORG_ADMIN이 자기 기관의 하위 이벤트 코드를 추가할 수 있게 하되 공통 상위분류는 운영자만 바꾸게 한다.

### 요구사항 9: 멱등성 키
**사용자 스토리:** 개발자로서, 같은 파일이 두 번 들어와도 자산이 하나만 생기길 원한다.

#### 수용 기준
1. THE Catalog_Store SHALL (organization_id, source_system_id, checksum_sha256)에 유일 제약을 둔다.
2. WHEN 같은 키의 자산 생성이 요청되면, THE Catalog_Store SHALL 새 자산을 만들지 않고 기존 asset_id를 반환한다.

### 요구사항 10: 품질 평가
**사용자 스토리:** 운영자로서, 품질 규칙을 코드 수정 없이 조정하고 싶다.

#### 수용 기준
1. THE Quality_Evaluator SHALL 설정 가능한 규칙(필수 필드, 촬영시각 품질, 재생 가능 여부, 값 범위)으로 quality_status와 사유 목록을 계산해 저장한다.
2. WHEN 규칙이 바뀌면, THE 시스템 SHALL 운영자 명령으로 기존 자산을 다시 평가할 수 있게 한다.

### 요구사항 11: 계보
**사용자 스토리:** 데이터셋 이용자로서, 데이터가 어디서 어떻게 만들어졌는지 추적하고 싶다.

#### 수용 기준
1. WHEN 파생 자산(프록시, 썸네일, 클립, 비식별본, 라벨)이 생성되면, THE Lineage SHALL 원 자산 ID·변환 종류·도구 또는 모델 버전·실행 시각·실행 주체를 기록한다.
2. WHEN `GET /v1/assets/{id}/lineage`를 받으면, THE 시스템 SHALL 상위·하위 자산을 모두 반환한다 (권한 범위 안에서).

### 요구사항 12: 시계열·공간
**사용자 스토리:** 개발자로서, 센서 시계열과 위치 검색을 같은 DB에서 처리하고 싶다.

#### 수용 기준
1. THE Observation_Store SHALL 관측값을 (organization_id, source_system_id, metric, observed_at, depth_m, channel, value, unit, original_value, original_unit, quality_flag, source_timestamp, clock_offset_ms, source_asset_id, received_at)로 월 단위 파티션 테이블에 저장하고 domain.md 7장의 중복 판단 키에 유일 제약을 둔다.
2. THE Catalog_Store SHALL location에 공간 인덱스를 두고 영역·반경 검색을 지원한다.
3. THE Catalog_Store SHALL 장면 임베딩용 벡터 컬럼을 준비한다 (값은 14번 스펙에서 채운다).

### 요구사항 13: 사전 검증 API
**사용자 스토리:** 외부 개발자로서, 실제로 보내기 전에 우리 데이터가 규격에 맞는지 확인하고 싶다.

#### 수용 기준
1. WHEN `POST /v1/contracts/validate`로 계약 이름(upload-manifest, observation-batch, event)과 문서를 받으면, THE Contract_Validator SHALL 저장하지 않고 스키마·코드표·도메인 규칙(위경도 순서, 시간대, 결측 표기 등)을 검사해 문제 목록을 반환한다.
2. THE Contract_Validator SHALL 운영 데이터와 같은 검증 코드를 쓴다 (별도 구현을 두지 않는다).

## 추적
- 기능 ID: STD-01, STD-02, STD-03, DB-01, DB-02, DB-03(준비), ONB-01(연계 카드 저장), ONB-02(장비 정책 모델), API-02(사전 검증)
- 수용시험: T05(T05-a, T05-b, T05-c, T05-f), T06(T06-e), T13(T13-b) — 세부는 docs/ACCEPTANCE.md
- 하드웨어 의존: 없음
