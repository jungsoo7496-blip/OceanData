# 설계: 14 선별 비식별·검수·태깅·장면 검색

## 이번에 구현한 것 (마이그레이션 0012)
| 구성 | 구현 |
|---|---|
| 대상 제한 | `deidentify` 작업은 13번 공유 심사가 PROCESSING으로 보낼 때(법률 검토 통과)와 검수 반려·미탐 재렌더링 때만 등록. 처리 시 share_status=PROCESSING 원본 영상이 아니면 SKIPPED. 일괄 처리 API 없음 |
| Detector | `adapters/detectors.py` DetectorPort: 기본 `SyntheticRegionDetector`(합성 시험용 모의), `OnnxRegionDetector`(Model_Registry 검사 후, 모델 확정 대기). 클래스 FACE·LICENSE_PLATE·VESSEL_ID_TEXT·SCREEN_TEXT를 `DEID_CLASSES`·작업 payload로 켜고 끔. 추적(`DEID_TRACKING`, 끊김 `DEID_TRACK_GAP_S`) — ADR-0020 |
| Mask_Renderer | `domain/deid.py` → ffmpeg `filter_complex`(BLUR·MOSAIC·FILL, 구간별 enable), 화면 밖 영역 자르기. 결과는 새 DEID 파생본(PROCESSED 존, 계보 relation=DEID), 원본 불변. 다시 렌더링하면 이전 비식별본은 review_result=FAILED |
| 처리 기록 | 파생본 `ext.deid`·`provenance.deid`: 모델 이름·버전·sha256·라이선스, 파라미터, 클래스별 탐지 수, 수동 영역 수, 처리 시각, "처리 이력" 안내 문구 |
| 검수 배정 | 처리 끝 → 원본 PROCESSING → QUALITY_REVIEW(SERVICE) + `deid_reviews` 배정(열린 배정이 적은 REVIEWER). 위험도 초안: 얼굴·번호판 탐지 또는 RESTRICTED 이상 HIGH, 다른 탐지 MEDIUM, 없음 LOW → `review_sampling_policies`(LOW 5%·최소 3, MEDIUM 20%·5, HIGH 100%·10) 비율로 표본 시각 |
| Review_Tool | `GET /v1/deid-reviews`(검수자는 자기 배정만, 운영자 전체), 상세, `…/frame?t=&which=ORIGINAL|DEID`(JPEG, 원본 열람 감사), `POST …/decision`: PASS → 파생본에만 review_result=PASSED·privacy_status=PSEUDONYMIZED(원본 아님, APPROVED는 13번 운영자). MISSED(클래스·시각 필수) → 수동 가림 영역이 있으면 REPROCESS → PROCESSING 재렌더링 등록, 같은 회차의 통과한 비식별 결과를 review_result=NULL + 재검수 배정. REJECT → 재처리 |
| ANONYMIZED_VERIFIED | `PATCH /v1/assets/{id}/classification`에서 운영자만, 파생본·legal_basis_ref 있을 때만 (요구사항 5.3) |
| 품질 집계 | `GET /v1/deid-quality`(운영자 전체, ORG_ADMIN 자기 기관): 클래스별 탐지·미탐·미탐률, 재처리 비율 (T09-c) |
| 태깅 | `tag_scenes` 작업(`PROCESSING_STEPS`에 TAG가 있으면 영상 처리 뒤): 표본 fps(`TAG_SAMPLE_FPS`) 밝기로 DAY/NIGHT, 객체는 모의 입력. `asset_tags`(MODEL/HUMAN), `PATCH /v1/assets/{id}/tags`(사람 수정·`asset_tag_edits` 이력). 정책 표 `tag_event_rules`(PERSON·FLOATING_OBJECT, 신뢰도 0.8 이상) → 이벤트 source=MODEL·미확인 |
| 장면 검색 | 같은 작업이 `EMBED_INTERVAL_S`(기본 2초)마다 태그 어휘 16차원 벡터를 `scene_embeddings`(pgvector, 모델 이름 포함)에 저장. `GET /v1/scenes/search?q=` → 한국어 낱말 → 어휘 → 코사인 거리, 권한 범위(scope_filter) 안 자산만 |
| CPU·측정 | 모두 CPU로 동작(GPU_ENABLED=false). `worker_frames_processed_total{job_type="deidentify"}` 등 초당 처리 프레임, `make bench`에 DEID_BLUR_CPU·DEID_MOSAIC_CPU 단계 |
| 화면 | '비식별 검수'(검수자·운영자): 배정 목록·위험도·처리 이력, 표본마다 원본/비식별본 나란히, 통과(표본 모두 본 뒤)·미탐(수동 영역)·반려, 품질 집계. 자산 화면 장면 검색 |

## 결정
- ADR-0020: 모델 확정 전 기준선(모의 탐지·밝기 주야·태그 어휘 임베딩), 포트 뒤에 둔다

## 확인 필요 / 다음과 연결
- 탐지·태깅·임베딩 모델 선정(라이선스·학습 데이터 조건)과 Model_Registry 등록, 모델별 전처리·후처리 (`OnnxRegionDetector`)
- 날씨 태그 관측값 보정(요구사항 6.3): 12번 Open API·07번 관측값 결합 후
- 표본 비율·위험도 기준·태그 → 이벤트 규칙 값은 초안 — 운영기관 확정
- 검수 결과로 다시 렌더링할 때 원본 공유 심사가 QUALITY_REVIEW가 아니면(예: 이미 APPROVED) 재렌더링하지 않는다. 승인 뒤 미탐 발견 시 처리 절차는 운영기관과 정한다
- REVIEWER가 자산 API로 원본 메타데이터를 보는 범위(배정 기반 범위 규칙)는 정하지 않았다 — 지금은 검수 API로만 본다
