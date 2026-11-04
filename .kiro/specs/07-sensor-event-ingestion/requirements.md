# 요구사항: 07 센서 관측값·실증 이벤트 수집

## 소개
센서·IoT 기업이 관측값을 보내고, 모든 기업이 실증 이벤트(선박·사람·부유물 탐지, 기상·파랑 상황, 장비 장애, 안전사고 등 기준 문서 7.2장의 분류)를 기록하는 수집 경로를 만든다. 시험 시작·종료 같은 업무 기록은 10번의 회차 기록이 맡는다.
기준 문서 8.1장의 "센서 IoT 어댑터(MQTT/REST/CSV) — 대상 기업 P0"(ING-08)를 구현한다.
센터는 HTTPS만 연다. MQTT는 현장 에이전트의 Sensor_Plugin이 구독해서 같은 REST 경로로 보낸다.

## 범위 밖
- 공공 Open API 관측값 수집 (12번)
- 영상 이벤트 클립 생성 (15번), 모델 기반 자동 탐지 (14번) — 이 스펙은 이벤트를 받는 쪽만
- 실시간 관제 화면

## 용어집
- **Observation_API**: `POST /v1/observations`, `GET /v1/observations`
- **Observation_Mapping**: 장비별 원천 필드 → 관측 항목(metric) 코드·원천 단위·시간 필드와 형식·source_timezone·결측 표기값 매핑 프로필
- **Sensor_File_Parser**: SENSOR 유형 파일(CSV·JSON)을 매핑 프로필로 읽어 관측값으로 바꾸는 작업
- **Sensor_Plugin**: 에이전트 확장 인터페이스(06번 요구사항 13)의 구현. MQTT 구독 → 관측값 배치 전송
- **Event_API**: `POST /v1/events`, `GET /v1/events`, `GET /v1/events/{id}`, `PATCH /v1/events/{id}`
- **Event_Code**: 공통 상위분류(기준 문서 7.2장: 선박·사람·부유물·기상/파랑·장비 장애·안전사고) + 기관별 하위코드. `contracts/codes/event-categories.json`

## 요구사항

### 요구사항 1: 관측값 배치 수신
**사용자 스토리:** 센서 기업으로서, 우리 서버나 수집기에서 관측값을 묶어서 보내고 싶다.

#### 수용 기준
1. WHEN `POST /v1/observations`로 `contracts/schemas/observation-batch.schema.json` 형식의 배치를 받으면, THE Observation_API SHALL 자격증명의 기관·장비 범위를 확인하고 항목별로 검증해 저장한다.
2. IF 배치 본문이 스키마에 맞지 않으면, THEN THE Observation_API SHALL 400 VALIDATION_ERROR를 반환하고 아무 항목도 저장하지 않는다.
3. WHEN 배치 형식이 맞으면, THE Observation_API SHALL 200과 항목별 결과(ACCEPTED, DUPLICATE, REJECTED + 사유 코드)를 반환한다.
4. IF 배치 항목 수가 설정 한도(기본 5,000, 정책값)를 넘으면, THEN THE Observation_API SHALL 스키마 검증보다 먼저 413 PAYLOAD_TOO_LARGE를 반환한다.
5. THE Observation_API SHALL Idempotency-Key를 받고 domain.md 7장 규칙을 따른다.

### 요구사항 2: 시간·단위·결측 규칙
**사용자 스토리:** 데이터 이용자로서, 기관마다 다른 시간대·단위·결측 표기를 믿고 쓸 수 있어야 한다.

#### 수용 기준
1. THE Observation_API SHALL observed_at을 시간대 오프셋이 있는 RFC 3339로 받는다. 오프셋 없는 원천 시각은 장비 정책의 source_timezone이 있을 때만 변환하고 원래 문자열을 source_timestamp에 보존한다.
2. IF 오프셋도 source_timezone도 없으면, THEN THE Observation_API SHALL 그 항목을 REJECTED(TIMEZONE_UNKNOWN)로 처리한다.
3. THE Normalizer(03번) SHALL 값과 단위를 `contracts/codes/units.json`으로 정규화하고 원래 값·단위를 보존한다. 사전에 없는 단위는 원래 값 그대로 저장하고 quality_flag=SUSPECT로 표시한다.
4. THE Observation_API SHALL value=null이면 quality_flag=MISSING을 요구하고, MISSING 항목에 값이 있으면 거부한다. 결측을 0이나 직전 값으로 채우지 않는다.
5. IF observed_at이 수신 시각보다 설정값(기본 10분) 이상 미래이면, THEN THE Observation_API SHALL 그 항목을 REJECTED(FUTURE_TIMESTAMP)로 처리한다.
6. WHEN 장비가 시계 오차(clock_offset_ms)를 보고하면, THE 시스템 SHALL 원래 observed_at을 바꾸지 않고 오차를 함께 기록하며, 설정 임계값을 넘으면 품질 경고를 남긴다.

### 요구사항 3: 중복과 정정
**사용자 스토리:** 센서 기업으로서, 재전송해도 값이 두 번 쌓이지 않길 원한다.

#### 수용 기준
1. WHEN (기관, 장비, metric, observed_at, depth_m, channel)이 같은 항목이 다시 오면, THE Observation_API SHALL 값이 같으면 DUPLICATE, 다르면 REJECTED(VALUE_CONFLICT)로 응답하고 기존 값을 덮어쓰지 않는다.
2. WHERE 값 정정이 필요하면, THE 시스템 SHALL ORG_ADMIN이 사유와 함께 새 값을 기록하게 하고 이전 값과 정정 이력을 보존한다.

### 요구사항 4: 매핑 프로필
**사용자 스토리:** 제공기업 담당자로서, 우리 장비의 원래 형식 그대로 보내고 싶다.

#### 수용 기준
1. THE 시스템 SHALL 장비별 Observation_Mapping을 버전과 함께 저장한다.
2. WHEN `GET /v1/devices/{id}/policy`를 받으면, THE 시스템 SHALL 그 장비의 현재 Observation_Mapping 버전을 함께 돌려준다.
3. THE 시스템 SHALL 운영자와 ORG_ADMIN이 매핑을 수정하게 하고, 수정 전후를 감사 로그에 남긴다.

### 요구사항 5: 센서 파일 파싱
**사용자 스토리:** 센서 기업으로서, 측정 장비가 만든 CSV를 폴더에 두기만 하면 관측값으로 들어가길 원한다.

#### 수용 기준
1. WHEN data_type=SENSOR인 CSV·JSON 자산이 REGISTERED가 되면, THE Job_Scheduler(05번) SHALL Sensor_File_Parser 작업을 등록한다.
2. THE Sensor_File_Parser SHALL 장비의 Observation_Mapping으로 파일을 읽어 요구사항 1~3과 같은 규칙으로 관측값을 저장하고, 각 관측값에 원본 asset_id를 연결한다.
3. IF 매핑이 없거나 파일 형식이 매핑과 맞지 않으면, THEN THE 시스템 SHALL 원본 파일은 유지하고 파싱 결과를 FAILED와 행 번호별 사유로 기록해 기관 담당자에게 알린다.
4. WHEN 매핑이 고쳐지면, THE 시스템 SHALL 같은 파일을 다시 파싱할 수 있게 하고 이전 결과와의 차이를 기록한다.

### 요구사항 6: MQTT 수집 (에이전트 플러그인)
**사용자 스토리:** MQTT를 쓰는 센서 기업으로서, 현장 브로커의 메시지가 그대로 플랫폼에 들어가길 원한다.

#### 수용 기준
1. THE Sensor_Plugin SHALL 현장 MQTT 브로커의 지정 토픽을 구독하고, 매핑 프로필로 관측값을 만들어 에이전트 대기열에 넣는다. 전송은 요구사항 1의 API로만 한다.
2. WHILE 서버에 연결할 수 없으면, THE Sensor_Plugin SHALL 관측값을 로컬에 쌓아 두고 연결이 복구되면 시간순으로 보낸다 (06번 오프라인 규칙).
3. THE 센터 SHALL MQTT 포트를 외부에 열지 않는다.
4. THE 통합 테스트 SHALL 개발용 MQTT 브로커 컨테이너와 모의 센서 발행기로 구독 → 배치 전송 → 저장을 확인한다. 실제 토픽·페이로드 형식은 연계 카드를 받은 뒤 정한다 ("확인 필요").

### 요구사항 7: 실증 이벤트
**사용자 스토리:** 제공기업 담당자로서, 탐지·장비 장애·안전사고 같은 사건을 시간 구간으로 남기고 영상과 연결하고 싶다.

#### 수용 기준
1. WHEN `POST /v1/events`로 `contracts/schemas/event.schema.json` 형식의 이벤트를 받으면, THE Event_API SHALL 회차·장비·시간 구간·Event_Code·출처(DEVICE, MANUAL, MODEL, RULE)를 검증해 저장한다.
2. THE Event_API SHALL 이벤트에 자산(클립·원본·문서)을 연결하고, 연결된 자산이 같은 기관 소속인지 확인한다.
3. WHERE 이벤트의 출처가 MODEL이면, THE 시스템 SHALL model_ref와 confidence를 요구하고 human_verified=false로 저장한다. 사람이 확인하면 확인자·시각과 함께 true로 바꾼다.
4. THE Event_API SHALL 원천 이벤트 ID가 있으면 domain.md 7장 규칙으로 중복을 막는다.
5. WHEN 기관 하위 이벤트 코드가 쓰이면, THE Event_API SHALL 그 코드가 공통 상위분류 아래에 등록되어 있는지 확인한다 (03번 Code_Table).
6. WHEN 이벤트에 시간 구간이 있으면, THE 시스템 SHALL 같은 회차에서 그 구간과 겹치는 자산 목록을 조회할 수 있게 한다.

### 요구사항 8: 조회
**사용자 스토리:** 제공기업 담당자로서, 회차별 관측값과 이벤트를 시간축으로 보고 싶다.

#### 수용 기준
1. WHEN `GET /v1/observations`를 받으면, THE Observation_API SHALL 장비·metric·기간·품질 필터와 커서 페이지네이션으로 자기 기관 관측값만 반환한다.
2. WHERE 요청에 집계 간격이 있으면, THE Observation_API SHALL 구간별 최소·최대·평균·건수와 결측 건수를 함께 반환한다 (결측을 평균에 0으로 넣지 않는다).
3. THE Event_API SHALL 회차·장비·코드·기간·human_verified 필터로 자기 기관 이벤트만 반환한다.

### 요구사항 9: 시험
#### 수용 기준
1. THE Sample_Generator(01번) SHALL 관측값 옵션을 추가로 제공해 합성 수온·파고 관측값(정상, 단위 오류, 시간대 없음, KST 표기, 결측, 중복, 미래 시각)을 만든다 (01번 변경 범위: 생성 옵션 추가만).
2. THE 통합 테스트 SHALL REST 배치, CSV 파일 파싱, MQTT 플러그인 세 경로가 같은 저장 결과를 내는지 확인한다.
3. THE 계약 시험 SHALL `contracts/examples/`의 관측값·이벤트 예시가 통과하고 `contracts/examples/invalid/` 예시가 거부되는지 확인한다 (T05).

## 추적
- 기능 ID: ING-08, STD-01(이벤트 코드표 일부), STD-02(시간·단위 정규화 일부), DB-02, POR-02(이벤트 관리 일부)
- 수용시험: T05(T05-a, T05-d), T06(T06-a), T07(T07-c), T13(T13-a) — 세부는 docs/ACCEPTANCE.md
- 하드웨어 의존: 없음
