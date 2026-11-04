# 요구사항: 08 저장·보존·백업

## 소개
존별 저장, 기관별 쿼터, 보존 정책 자동 실행, 법적 보류, 삭제 요청과 삭제 증명, 계층형 백업, 복원 시험, 용량 예측을 만든다.
모든 수치는 정책 테이블 값이며 스토리지 제품에 의존하지 않는다.

## 범위 밖
- 스토리지 제품 선정과 실제 하드웨어 확장 시험 (T18, 하드웨어 후)
- RAW 전체의 독립 백업 — 하지 않는다. backup_class=PROTECTED로 올린 자산만 백업한다 (domain.md 9장)
- 요금·정산 (사용량 리포트만 만든다)

## 용어집
- **Storage_Service**: StoragePort를 쓰는 저장 서비스
- **Quota_Manager**: 기관별 사용량·쿼터 관리
- **Retention_Engine**: 보존 정책을 매일 실행하는 작업
- **Legal_Hold**: 삭제 보류 표시
- **Deletion_Service**: 삭제 요청 처리와 삭제 증명 생성
- **Backup_Job**: 독립 백업 작업
- **Capacity_Forecaster**: 용량 추세 예측
- **Tier_Mover**: Hot/Warm 계층 사이로 객체를 옮기는 작업
- **Usage_Report**: 기관별 월간 자원 사용 리포트

## 요구사항

### 요구사항 1: 존과 객체 키
**사용자 스토리:** 개발자로서, 어떤 스토리지 제품이 와도 같은 구조로 저장하고 싶다.

#### 수용 기준
1. THE Storage_Service SHALL domain.md 9장의 존별 버킷과 객체 키 규칙을 사용하며 버킷 이름은 설정값이다.
2. THE Storage_Service SHALL 원래 파일명을 객체 키에 넣지 않는다.
3. WHERE 스토리지가 서버 측 암호화를 지원하면, THE Storage_Service SHALL 설정에 따라 암호화를 요청한다.
4. IF 스토리지가 서버 측 암호화를 지원하지 않으면, THEN THE 시스템 SHALL 기동 시 경고하고 `/readyz` 상세에 표시한다.

### 요구사항 2: 쿼터
**사용자 스토리:** 운영기관으로서, 한 기관이 저장 공간을 다 쓰지 않게 하고 싶다.

#### 수용 기준
1. THE Quota_Manager SHALL 기관별 쿼터(바이트)를 정책 테이블로 관리한다.
2. THE Quota_Manager SHALL 기관 사용량을 domain.md 9장의 식(RAW·PROCESSED·EVIDENCE·QUARANTINE 객체 + 업로드 예약량 + 신청 백업 용량)으로 계산하고 항목별로 보여준다.
3. THE Quota_Manager SHALL 하루 한 번 스토리지 실측과 대조하고 차이가 있으면 운영자에게 알린다.
4. WHEN 사용률이 80%, 90%, 100%에 도달하면, THE Quota_Manager SHALL ORG_ADMIN에게 알린다.
5. THE Quota_Manager SHALL 04번이 만든 예약·확정·해제를 그대로 쓰고, 이 스펙에서는 알림·일일 대조·리포트만 더한다.
6. THE Quota_Manager SHALL 신청 백업 용량을 기관 사용량 안의 별도 항목으로 보여준다.

### 요구사항 3: 보존 정책
**사용자 스토리:** 운영기관으로서, 원본은 기간이 지나면 지우고 프록시와 클립은 남기고 싶다.

#### 수용 기준
1. THE 시스템 SHALL 대상(RAW, PROXY, THUMBNAIL, CLIP, DEID, EVIDENCE, DATASET, QUARANTINE)별 보존 기간과 만료 동작(DELETE 또는 KEEP)을 정책 테이블로 관리한다. 격리는 사유별로 다르며 UNKNOWN_SESSION·MISSING_REQUIRED_METADATA는 자동 삭제하지 않는다 (domain.md 9장).
2. WHEN 자산이 등록되면, THE 시스템 SHALL retention_policy_id와 retention_until을 계산해 저장한다.
3. THE Retention_Engine SHALL 매일 실행되어 만료 N일 전(기본 30일)에 ORG_ADMIN에게 알린다.
4. WHEN retention_until이 지나면, THE Retention_Engine SHALL 만료 동작을 실행하고 삭제 기록을 남긴다.
5. THE Retention_Engine SHALL 데이터셋 버전의 사본(DATASET 존)을 원 자산의 만료와 별개로 다룬다.
6. WHEN 만료 삭제를 실행하기 전, THE Retention_Engine SHALL dry-run 결과(대상 수·용량, 법적 보류로 제외된 수, 승인된 증빙·데이터셋이 인용 중인 자산 경고)를 만들고 정책에 따라 운영자 확인 후 실행한다.
7. WHEN 운영자가 `POST /v1/retention/dry-run`을 요청하면, THE 시스템 SHALL 삭제 없이 같은 형식의 결과를 반환한다.

### 요구사항 4: 법적 보류
**사용자 스토리:** 운영자로서, 분쟁·조사 중인 데이터가 자동 삭제되지 않게 하고 싶다.

#### 수용 기준
1. WHILE 자산에 Legal_Hold가 설정되어 있으면, THE Retention_Engine과 Deletion_Service SHALL 해당 자산을 삭제하지 않는다.
2. THE 시스템 SHALL Legal_Hold 설정·해제를 운영자만 사유와 함께 할 수 있게 하고 감사 로그에 남긴다.

### 요구사항 5: 삭제 요청
**사용자 스토리:** 제공기업 관리자로서, 우리 데이터를 지우고 지웠다는 증명을 받고 싶다.

#### 수용 기준
1. WHEN ORG_ADMIN이 자산 또는 실증회차 단위로 삭제를 요청하면, THE Deletion_Service SHALL 활성 객체를 삭제하고 삭제 증명(대상, 시각, 요청자, 객체 키 목록, sha256)을 만든다.
2. THE 삭제 증명 SHALL 독립 백업에 남은 사본이 백업 보존 주기가 끝나면 사라진다는 사실과 그 날짜를 적는다.
3. IF 대상 자산이 유효한 이용 권한(GRANTED)이 있는 데이터셋 버전에 포함되어 있으면, THEN THE Deletion_Service SHALL 409 CONFLICT로 삭제를 거부하고 철회 절차를 안내한다.
4. IF 대상 자산을 승인된 증빙·보고서가 인용하고 있으면, THEN THE Deletion_Service SHALL 영향 목록을 보여주고 ORG_ADMIN의 재확인을 받은 뒤 삭제하며, 해당 증빙·보고서에 근거 손상 표시가 남게 한다 (10번).

### 요구사항 6: 계층형 백업
**사용자 스토리:** 운영기관으로서, 다시 만들 수 없는 데이터만 효율적으로 백업하고 싶다.

#### 수용 기준
1. THE Backup_Job SHALL 메타데이터 DB, 감사 로그, EVIDENCE·DEID·DATASET 객체, backup_class=PROTECTED 자산, 정책·설정을 별도 백업 대상(S3 엔드포인트 설정값)으로 정기 백업한다.
2. THE Backup_Job SHALL backup_class=NONE인 RAW 객체를 백업하지 않는다.
3. WHEN 백업이 끝나면, THE Backup_Job SHALL 결과(성공·실패·대상 수·용량)를 기록하고 실패하면 운영자에게 알린다.
4. WHEN ORG_ADMIN이 local_copy_policy가 ROTATING·NONE·UNKNOWN인 원천 장비의 자산 백업을 신청하면, THE 시스템 SHALL 예상 용량을 보여준 뒤 대상 자산을 PROTECTED로 바꾸고 백업 용량을 그 기관 사용량에 반영한다.
5. THE Backup_Job SHALL backup_class가 PROTECTED로 바뀐 자산(10번 승인, 요구사항 6.4 신청)을 다음 백업에 포함한다.

### 요구사항 7: 복원 시험
**사용자 스토리:** 운영자로서, 백업이 실제로 복원되는지 정기적으로 확인하고 싶다.

#### 수용 기준
1. WHEN 운영자가 `make restore-test`를 실행하면, THE 시스템 SHALL 임시 환경에 백업을 복원하고 DB 행 수·객체 해시를 원본과 대조해 결과를 보고한다 (T10).
2. THE 저장소 SHALL 복원 절차를 문서(Runbook)로 제공한다.

### 요구사항 8: 용량 예측
**사용자 스토리:** 운영기관으로서, 스토리지 증설 시점을 미리 알고 싶다.

#### 수용 기준
1. THE Capacity_Forecaster SHALL 일별 사용량 추세로 전체 사용률이 설정값(기본 70%)에 도달할 예상일을 계산한다.
2. WHEN 예상일이 설정한 리드타임(기본 180일) 안으로 들어오면, THE Capacity_Forecaster SHALL 운영자에게 알린다.

### 요구사항 9: Hot/Warm 계층
**사용자 스토리:** 운영기관으로서, 자주 보는 데이터는 빠른 저장소에, 오래된 원본은 저렴한 저장소에 두고 싶다.

#### 수용 기준
1. THE Storage_Service SHALL 계층(HOT, WARM)을 설정값으로 정의한다. 계층마다 S3 엔드포인트·버킷을 따로 지정할 수 있고 같은 저장소를 함께 쓸 수도 있다.
2. THE Tier_Mover SHALL 정책 테이블의 규칙(대상 존·변형, 경과 일수, 마지막 조회 후 일수)으로 객체를 다른 계층에 복사하고, sha256이 같은지 확인한 뒤에만 원래 객체를 지우고 위치 정보를 갱신한다.
3. THE 시스템 SHALL 계층 이동 중에도 조회와 다운로드 URL 발급이 끊기지 않게 한다 (이동이 끝나기 전까지 원래 위치를 쓴다).
4. THE Tier_Mover SHALL 스토리지 제품 전용 자동 계층화 기능에 의존하지 않는다 (tech.md HW-1).

### 요구사항 10: 기관별 자원 사용 리포트 (P1)
**사용자 스토리:** 운영기관으로서, 기관별로 저장·처리 자원을 얼마나 쓰는지 알고 싶다.

#### 수용 기준
1. THE Usage_Report SHALL 기관별 월간 사용량(존·계층별 저장량, 백업 대상 용량, 처리 작업 시간(CPU·GPU 구분), 다운로드 용량)을 만들어 ORG_ADMIN과 운영자에게 제공한다.
2. THE Usage_Report SHALL 금액이 아니라 사용량만 담는다.

## 추적
- 기능 ID: STO-01~06, OPS-02(용량 예측), OPS-04
- 수용시험: T03(T03-c), T06(T06-c), T10(T10-a, T10-b), T12(T12-a), T15(T15-a, T15-b, T15-c, T15-f) — 세부는 docs/ACCEPTANCE.md
- 하드웨어 의존: 없음 (실제 확장 시험 T18은 하드웨어 후)
