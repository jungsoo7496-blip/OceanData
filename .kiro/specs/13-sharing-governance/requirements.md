# 요구사항: 13 공유 등급·데이터셋·승인형 배포

## 소개
기업이 동의한 부산물(BYPRODUCT) 데이터만 심사·비식별·검수를 거쳐 데이터셋으로 묶고, 승인된 이용기업에 기한부로 제공한다.
공유 가능 조건은 domain.md 4장을 도메인 계층 한 곳에서 강제한다.

## 범위 밖
- 비식별·검수 처리 자체 (14번) — 이 스펙은 흐름과 상태만
- 결제·정산·크레딧 — 제외
- 격리 분석 환경 — P2, 별도 스펙

## 용어집
- **Sharing_Rules**: domain.md 4장을 구현한 도메인 규칙
- **Share_Request**: 공유 등급 변경 요청
- **Review_Flow**: CANDIDATE → LEGAL_REVIEW → PROCESSING → QUALITY_REVIEW → APPROVED 흐름
- **Dataset_Builder**: 승인 자산으로 데이터셋 버전을 만드는 구성요소
- **Data_Card**: 데이터셋 설명서
- **Catalog**: 이용기업용 데이터셋 목록
- **Access_Request**: 이용 신청
- **Grant**: 기한부 이용 권한
- **Distribution**: 파일 제공 (단기 다운로드 URL, API)
- **DCAT_Exporter**: 공개 데이터셋 메타데이터를 DCAT 3로 내보내는 구성요소

## 요구사항

### 요구사항 1: 공유 후보 지정
**사용자 스토리:** 제공기업 담당자로서, 공유해도 괜찮은 환경 영상을 골라 후보로 올리고 싶다.

#### 수용 기준
1. WHEN ORG_ADMIN 또는 ORG_MEMBER가 자산 또는 실증회차를 공유 후보로 지정하면, THE 시스템 SHALL data_role=BYPRODUCT인 자산만 share_status=CANDIDATE로 바꾼다.
2. IF 대상에 data_role=CORE 자산이 있으면, THEN THE 시스템 SHALL 그 자산만 제외하고 200과 제외된 자산 목록·사유를 반환한다.

### 요구사항 2: data_role 변경
**사용자 스토리:** 제공기업 관리자로서, 핵심과 부산물 구분을 우리가 결정하고 싶다.

#### 수용 기준
1. THE 시스템 SHALL ORG_ADMIN만 data_role을 바꾸게 하고 사유를 필수로 받는다.
2. WHEN data_role이 BYPRODUCT에서 CORE로 바뀌면, THE 시스템 SHALL sharing_scope를 L1 이하로 낮추고 하향 처리(요구사항 10)를 실행한다.

### 요구사항 3: 공유 조건 강제
**사용자 스토리:** 운영기관으로서, 사람이 실수해도 조건에 맞지 않는 데이터가 공유되지 않게 하고 싶다.

#### 수용 기준
1. WHEN `POST /v1/assets/{id}/share-requests`로 목표 등급을 받으면, THE Sharing_Rules SHALL domain.md 4장 조건을 평가한다.
2. IF 조건을 하나라도 충족하지 못하면, THEN THE 시스템 SHALL 422 RULE_VIOLATION과 미충족 조건 목록을 반환한다.
3. THE 시스템 SHALL API·워커·화면·관리 명령 어디서든 sharing_scope를 바꿀 때 Sharing_Rules를 거치게 한다 (우회 경로 없음).
4. THE 테스트 SHALL 무작위 속성 조합을 생성하는 속성 기반 테스트로 data_role=CORE 자산이 L2 이상이 되는 경로가 없음을 검증한다 (T08).
5. IF 제공기관의 2단 공동활용 약정이 없거나 만료되었으면, THEN THE Sharing_Rules SHALL L2 이상 요청을 RULE_VIOLATION(제공기관 2단 약정 유효)으로 거부한다.
6. THE Sharing_Rules SHALL 파생 자산이 원 자산들의 가장 엄격한 data_role·sensitivity_class를 물려받게 하고, 원본(variant=ORIGINAL)과 CORE 원본의 파생본을 L1 위로 올리지 않는다.
7. THE 속성 기반 테스트(T08-c) SHALL 파생 관계(원본 → 비식별본 → 데이터셋 사본)를 포함한 무작위 조합으로도 domain.md 4장 조건을 깨는 경로가 없음을 검증한다.
8. IF L1 이상 변경을 ORG_ADMIN이 아닌 주체가 요청하면, THEN THE 시스템 SHALL 403 FORBIDDEN을 반환한다.

### 요구사항 4: 심사 흐름
**사용자 스토리:** 운영자로서, 공유 후보를 정해진 단계로 심사하고 싶다.

#### 수용 기준
1. THE Review_Flow SHALL share_status를 domain.md 5장 전이표의 순서와 주체로만 바꾼다.
2. WHEN 운영자가 LEGAL_REVIEW를 통과시키면, THE 시스템 SHALL legal_basis_ref 기록을 필수로 받는다.
3. WHEN 자산이 PROCESSING에 들어가면, THE 시스템 SHALL 비식별 작업(14번)을 등록한다.
4. IF 어느 단계에서든 반려되면, THEN THE 시스템 SHALL REJECTED 또는 REPROCESS와 사유를 기록하고 기관에 알린다.
5. WHEN 원본이 APPROVED가 되면, THE 시스템 SHALL 그 심사에서 만든 파생본에 share_status=APPROVED와 review_result=PASSED를 기록한다. privacy_status=PSEUDONYMIZED 같은 처리 결과도 파생본에만 기록한다.
6. THE 시스템 SHALL 운영자가 sensitivity_class와 privacy_status=NOT_APPLICABLE을 근거(점검 기록 참조)와 함께 지정하게 하고 감사 로그에 남긴다.

### 요구사항 5: 데이터셋 버전
**사용자 스토리:** 이용기업으로서, 바뀌지 않는 데이터셋 버전을 받고 싶다.

#### 수용 기준
1. WHEN 운영자가 APPROVED 자산으로 데이터셋 버전을 만들면, THE Dataset_Builder SHALL 자산 사본을 DATASET 존에 두고 파일 목록과 sha256을 담은 매니페스트를 만든다.
2. THE 데이터셋 버전 SHALL 만든 뒤 수정할 수 없다. 변경은 새 버전으로만 한다.
3. THE Dataset_Builder SHALL 데이터셋 버전에 포함된 자산의 계보를 유지한다.

### 요구사항 6: 데이터 카드
**사용자 스토리:** 이용기업으로서, 데이터가 어디서 어떻게 만들어졌는지 알고 싶다.

#### 수용 기준
1. THE Data_Card SHALL 기준 문서 18.2장의 항목(권리자, 원천 기관, 기간·해역(일반화), 규모, 환경 조건 분포, 한계, 비식별 방법, 계보, 이용 조건 등)을 담는다.
2. IF 필수 항목이 비어 있으면, THEN THE 시스템 SHALL 데이터셋 게시를 막는다.

### 요구사항 7: 카탈로그
**사용자 스토리:** 이용기업으로서, 받을 수 있는 데이터셋을 찾아보고 싶다.

#### 수용 기준
1. THE Catalog SHALL CONSUMER에게 게시된 데이터셋의 Data_Card, 통계, 샘플 썸네일만 보여준다.
2. THE 시스템 SHALL 승인되지 않은 자산을 어떤 카탈로그·검색 API로도 노출하지 않는다.

### 요구사항 8: 이용 신청과 승인
**사용자 스토리:** 이용기업으로서, 목적과 기간을 밝히고 데이터 이용을 신청하고 싶다.

#### 수용 기준
1. WHEN CONSUMER가 `POST /v1/access-requests`로 데이터셋 버전·목적·기간·보안 조치를 적고 이용 약정에 동의해 신청하면, THE 시스템 SHALL Access_Request를 REQUESTED로 만든다.
2. THE 시스템 SHALL 데이터셋에 포함된 모든 제공기관 ORG_ADMIN의 승인을 받은 뒤 운영자 최종 승인을 받게 한다 (`POST /v1/access-requests/{id}/decisions`).
3. IF 설정한 응답 기한 안에 승인이 없으면, THEN THE 시스템 SHALL 기본값으로 거부(DENIED) 처리한다.
4. WHEN 최종 승인되면, THE 시스템 SHALL 기간이 정해진 Grant를 만든다.
5. IF 신청 목적이 데이터셋에 포함된 제공기관 중 한 곳이라도 2단 약정에서 허용하지 않은 목적이면, THEN THE 시스템 SHALL 신청을 RULE_VIOLATION으로 거부하고 허용되지 않은 기관 수만 알려준다 (기관 이름은 알려주지 않는다).
6. IF 제공기관 한 곳이라도 거절하면, THEN THE 시스템 SHALL 신청을 DENIED로 끝낸다. 운영자 승인으로 이 결정을 뒤집을 수 없다.
7. THE Grant SHALL 승인된 데이터셋 버전 하나에만 적용된다. 새 버전에는 새 신청이 필요하다.

### 요구사항 9: 배포
**사용자 스토리:** 이용기업으로서, 승인된 기간 동안 데이터를 받고 싶다.

#### 수용 기준
1. WHILE Grant가 유효하면, THE Distribution SHALL `POST /v1/distributions`로 파일별 단기 다운로드 URL(기본 5분)과 매니페스트를 발급한다.
2. WHEN 다운로드 URL을 발급하면, THE 시스템 SHALL 감사 로그와 제공기관 이용 이력에 남긴다.
3. THE Distribution SHALL 다운로드 묶음에 이용자·Grant 식별 정보를 담은 매니페스트를 넣는다.

### 요구사항 10: 만료·철회·하향
**사용자 스토리:** 제공기업 관리자로서, 공유를 언제든 멈출 수 있어야 한다.

#### 수용 기준
1. WHEN Grant 기간이 끝나면, THE 시스템 SHALL grant_status를 EXPIRED로 바꾸고 새 URL 발급을 멈춘다.
2. WHEN ORG_ADMIN 또는 운영자가 철회하면, THE 시스템 SHALL 즉시 새 URL 발급을 멈추고 이용자에게 알리며 파기 확인 제출 상태를 관리한다.
3. WHEN ORG_ADMIN이 `POST /v1/assets/{id}/share-requests`에 낮은 목표 등급과 existing_grant_handling을 보내 공유 등급을 낮추면, THE 시스템 SHALL 이후 새 데이터셋 버전에서 해당 자산을 빼고, 기존 Grant 처리 방식(즉시 철회 또는 만료까지 유지) 선택을 기록한다.
4. THE 시스템 SHALL 이미 다운로드된 파일은 회수되지 않는다는 사실을 철회 화면과 통지에 명시한다.

### 요구사항 11: 이용 이력
**사용자 스토리:** 제공기업으로서, 우리 데이터를 누가 언제 무슨 목적으로 받았는지 보고 싶다.

#### 수용 기준
1. THE 시스템 SHALL 제공기관이 자기 자산이 포함된 데이터셋의 신청·승인·다운로드 이력을 조회하게 한다.

### 요구사항 12: 공개 메타데이터 내보내기
**사용자 스토리:** 운영기관으로서, 나중에 광역 데이터 허브와 연계할 수 있게 준비하고 싶다.

#### 수용 기준
1. WHEN `GET /v1/catalog/dcat`를 받으면, THE DCAT_Exporter SHALL 모든 항목이 L4인 데이터셋(domain.md 4장)의 메타데이터만 DCAT 3 JSON-LD로 반환한다.

### 요구사항 13: 화면
**사용자 스토리:** 사용자로서, 위 기능을 웹에서 쓰고 싶다.

#### 수용 기준
1. THE Workspace SHALL 공유 후보 지정, 등급 변경 요청(미충족 조건 표시), 이용 신청 승인, 이용 이력 화면을 제공한다.
2. THE Operator_Console SHALL 심사 단계 처리, 데이터셋 구성, 데이터 카드 작성, 최종 승인 화면을 제공한다.
3. THE 웹 SHALL CONSUMER용 카탈로그·신청·다운로드 화면을 제공한다.

## 추적
- 기능 ID: SEC-03, POR-05, ADM-03, CAT-03, SHR-01, SHR-02
- 수용시험: T01(T01-e, T01-g), T06(T06-b, T06-c), T08(T08-a, T08-b, T08-c, T08-d, T08-e, T08-f, T08-g, T08-h, T08-i, T08-j), T15(T15-d, T15-e, T15-g) — 세부는 docs/ACCEPTANCE.md
- 하드웨어 의존: 없음
