# 요구사항: 05 처리 파이프라인 (작업 큐·메타 추출·프록시·무결성 로그)

## 소개
등록된 자산을 비동기로 처리하는 파이프라인을 만든다.
PostgreSQL 기반 작업 큐, 우선순위·자원 클래스 스케줄러, 영상 메타 추출, 웹 재생용 프록시와 썸네일, 무결성 로그, 추론 백엔드 추상화를 포함한다.
GPU가 없어도 모든 단계가 CPU로 동작해야 한다.

## 범위 밖
- 비식별·태깅·임베딩 작업 내용 (14번) — 이 스펙은 작업 유형과 스케줄링 틀만
- 엣지에서 만든 프록시 수신 (15번)

## 용어집
- **Job_Queue**: PostgreSQL 작업 테이블 기반 큐 (QueuePort 구현)
- **Worker**: 작업을 가져가 처리하는 프로세스
- **Job_Scheduler**: 우선순위·자원 클래스로 작업을 배정하는 구성요소
- **Media_Probe**: ffprobe로 영상 정보를 읽는 구성요소
- **Proxy_Builder**: 웹 재생용 저화질 사본을 만드는 구성요소
- **Thumbnail_Builder**: 대표 프레임 이미지를 만드는 구성요소
- **Integrity_Log**: 자산 등록 해시를 체인으로 기록하는 추가 전용 로그
- **Inference_Backend**: InferencePort 구현 (ONNX Runtime CPU/CUDA)
- **Model_Registry**: 모델 이름·버전·sha256·라이선스 기록

## 요구사항

### 요구사항 1: 작업 큐
**사용자 스토리:** 개발자로서, 외부 브로커 없이 안정적인 작업 큐를 쓰고 싶다.

#### 수용 기준
1. THE Job_Queue SHALL 작업을 (유형, 대상 자산, 우선순위, 자원 클래스 CPU/GPU, 상태, 시도 횟수, 다음 실행 시각, 오류)로 PostgreSQL 테이블에 저장한다.
2. WHEN 여러 Worker가 동시에 작업을 가져가면, THE Job_Queue SHALL 같은 작업이 두 Worker에 배정되지 않게 한다 (`FOR UPDATE SKIP LOCKED`).
3. IF Worker가 작업 도중 중단되면, THEN THE Job_Queue SHALL 임대 시간이 지난 작업을 다시 대기 상태로 돌린다.

### 요구사항 2: 기본 파이프라인
**사용자 스토리:** 제공기업 담당자로서, 올린 영상을 바로 웹에서 보고 싶다.

#### 수용 기준
1. WHEN 영상 자산이 REGISTERED가 되면, THE Job_Scheduler SHALL 메타 추출 → 프록시 생성 → 썸네일 생성 작업을 순서대로 등록한다.
2. WHEN 프록시와 썸네일이 완료되면, THE 시스템 SHALL ingest_status를 PROXIED를 거쳐 READY_PRIVATE로 바꾼다.
3. WHERE 자산이 영상이 아니면, THE Job_Scheduler SHALL 해당 유형에 필요 없는 단계를 건너뛰고 REGISTERED에서 READY_PRIVATE로 바꾼다.
4. THE Job_Scheduler SHALL 다른 스펙이 작업 유형을 등록할 수 있게 한다 (예: 07번 Sensor_File_Parser, 14번 비식별).
5. WHERE 자산이 바이너리 없는 C0 자산이면, THE Job_Scheduler SHALL 메타 추출·프록시 단계를 등록하지 않는다.

### 요구사항 3: 메타 추출
**사용자 스토리:** 데이터 이용자로서, 영상의 해상도·길이·코덱을 정확히 알고 싶다.

#### 수용 기준
1. THE Media_Probe SHALL 길이, 해상도, fps, 코덱, 비트레이트, 컨테이너 내부의 생성 시각 태그, GPS 정보(있으면)를 추출해 video_ext와 촬영시각 근거에 반영한다.
2. WHEN 컨테이너 내부 생성 시각 태그로 촬영시각을 채우면, THE Media_Probe SHALL timestamp_quality.source=DEVICE, verified=false로 기록한다.
3. IF 파일을 읽을 수 없거나 재생할 수 없으면, THEN THE 시스템 SHALL 자산을 QUARANTINED(CORRUPT_FILE)로 바꾼다.

### 요구사항 4: 프록시와 썸네일
**사용자 스토리:** 제공기업 담당자로서, 수 GB 원본을 내려받지 않고 영상을 확인하고 싶다.

#### 수용 기준
1. THE Proxy_Builder SHALL 설정 프로파일(기본 H.264, 짧은 변 480픽셀, 비트레이트 상한, 스트리밍 재생용 배치)로 프록시를 만들어 PROCESSED 존에 저장한다.
2. WHERE GPU_ENABLED=true이고 하드웨어 인코더를 쓸 수 있으면, THE Proxy_Builder SHALL 하드웨어 인코더를 쓰고, 아니면 CPU로 인코딩한다.
3. THE Thumbnail_Builder SHALL 설정 간격으로 대표 프레임 이미지를 만든다.
4. WHEN 프록시·썸네일을 만들면, THE 시스템 SHALL 원 자산과의 계보(변환 종류, 도구 버전, 실행 시각)를 기록한다.

### 요구사항 5: 무결성 로그
**사용자 스토리:** 제공기업으로서, 등록 이후 데이터가 바뀌지 않았다는 증거를 갖고 싶다.

#### 수용 기준
1. THE Integrity_Log SHALL 04번이 추가하는 등록 항목과 파생 자산 등록 항목을 같은 해시 체인으로 관리한다.
2. THE Integrity_Log SHALL 하루 단위로 앵커 해시를 만든다.
3. WHEN 운영자가 검증 명령을 실행하면, THE 시스템 SHALL 체인 단절과, 표본 객체의 현재 해시와 기록 해시의 불일치를 보고한다.

### 요구사항 6: 우선순위와 자원 클래스
**사용자 스토리:** 운영자로서, GPU 한 대를 가장 중요한 작업에 먼저 쓰고 싶다.

#### 수용 기준
1. THE Job_Scheduler SHALL 정책 테이블의 우선순위(기본: DEID > PROXY > TAG > EMBED > REPROCESS)로 작업을 배정한다.
2. WHERE 작업이 GPU 클래스이고 GPU Worker가 없으면, THE Job_Scheduler SHALL 정책에 따라 CPU로 실행하거나 대기시킨다.
3. THE Job_Scheduler SHALL 처리 지연·적체가 업로드 API 응답에 영향을 주지 않게 한다.

### 요구사항 7: 실패와 재처리
**사용자 스토리:** 운영자로서, 실패한 작업을 원인과 함께 확인하고 다시 돌리고 싶다.

#### 수용 기준
1. IF 작업이 실패하면, THEN THE Job_Queue SHALL 지수 백오프로 최대 N회(설정값) 재시도하고, 그래도 실패하면 FAILED와 사유를 기록한다.
2. WHEN 운영자가 `POST /v1/assets/{id}/reprocess`를 요청하면, THE 시스템 SHALL 지정한 단계를 다시 실행하고 새 파생 자산을 계보에 연결한다.

### 요구사항 8: 추론 백엔드
**사용자 스토리:** 개발자로서, GPU가 들어오기 전에도 AI 단계를 개발하고 싶다.

#### 수용 기준
1. THE Inference_Backend SHALL 모델 로드와 배치 추론을 InferencePort 뒤에서 제공하며 기본은 CPU 실행이다.
2. WHERE GPU_ENABLED=true이면, THE Inference_Backend SHALL CUDA 실행 공급자를 사용한다.
3. THE Inference_Backend SHALL Model_Registry에 이름·버전·sha256·라이선스가 기록된 모델만 로드한다.
4. IF 모델의 라이선스가 허용 목록에 없거나 기록되지 않았으면, THEN THE Inference_Backend SHALL 로드를 거부한다.

### 요구사항 9: 측정
**사용자 스토리:** 운영자로서, 하드웨어 구매 전에 처리 속도를 실측하고 싶다.

#### 수용 기준
1. THE Worker SHALL 작업 유형별 처리 시간, 초당 처리 프레임, 대기 길이, 실패율을 `/metrics`에 노출한다.
2. WHEN 개발자가 `make bench`를 실행하면, THE 시스템 SHALL 합성 영상으로 단계별 처리량을 측정해 JSON 보고서를 만든다 (T16).

## 추적
- 기능 ID: PRC-01, PRC-02, PRC-03, PRC-04, PRC-10(재처리 틀)
- 수용시험: T04(T04-f), T06(T06-e), T12(T12-c), T16(T16-a, T16-b, T16-c) — 세부는 docs/ACCEPTANCE.md
- 하드웨어 의존: 없음 (GPU는 선택)
