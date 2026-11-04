---
inclusion: always
---

# 도메인 규칙 (계약)

이 문서의 값과 규칙은 모든 스펙과 `contracts/`가 그대로 따른다.
바꾸려면 이 문서부터 고치고, `contracts/`를 맞춘 뒤 `make contracts-check`를 통과시킨다 (이 문서와 스키마의 코드값이 다르면 검사가 실패한다).

## 1. 계층과 장비
기관(Organization, `org_`) → 실증사업(Project, `prj_`) → 실증회차(Session, `ses_`) → 장비(Device, `dev_`) → 자산(Asset, `ast_`)
- 업로드(Upload, `upl_`)는 전송 작업 단위이며 실증회차와 다르다.
- 장비는 두 종류다. **원천 장비**(카메라·NVR·센서·공공 관측소 등 데이터를 만드는 장비)는 자산·관측값·이벤트의 source_system_id가 된다. **수집기**(에이전트를 설치한 PC·엣지 박스)는 매핑된 원천 장비 여러 대의 데이터를 보낸다. 수집기가 스스로 원천이 될 수도 있다.
- 기기 자격증명은 수집기에 발급한다. 수집기는 자기에게 매핑된 원천 장비의 데이터만 보낼 수 있다.
- 실증 이벤트(Event, `evt_`)는 회차·장비·시간 구간에 붙는 기록이며 자산(클립 등)과 연결된다.
- 관측값(Observation)은 원천 장비가 만든 시계열 한 점이다. 원본 파일(CSV 등)에서 나왔으면 그 자산을 참조한다. 공공 관측소는 원천 장비(EXTERNAL_STATION)로 등록한다.
- 파생 자산은 다른 자산에서 만든 자산이다 (variant ≠ ORIGINAL). 원 자산 목록은 provenance.parent_asset_ids에 있다.
- 데이터셋(Dataset, `dst_`)과 데이터셋 버전(DatasetVersion, `dsv_`, 수정 불가)
- 기관 유형(org_type): PROVIDER(실증기업) / PROGRAM_AGENCY(관리기관) / OPERATOR(운영기관) / CONSUMER(이용기업)
- 기관 약정 (기준 문서 14.1장): 1단 수집·관리 약정(처리위탁, L0·L1 대상)과 2단 공동활용 제공 약정(L2~L4 대상). 기관마다 약정 참조·유효기간·허용 목적(purpose 코드표)을 기록한다.

## 2. 역할과 데이터 범위
| 코드 | 이름 | 하는 일 |
|---|---|---|
| ORG_ADMIN | 제공기업 관리자 | 자기 기관 전체, 공유 등급 결정, 이용 신청 1차 승인 |
| ORG_MEMBER | 제공기업 담당자 | 자기 기관 데이터 조회·업로드·보고서 작성 (공유 결정 불가) |
| PROGRAM_VIEWER | 관리기관 열람자 | 배정된 실증사업의 제출 자료 열람 |
| PLATFORM_OPERATOR | 운영자 | 기관·장비·정책 관리, 공유 심사, 공유 최종 승인 |
| REVIEWER | 검수자 | 배정된 비식별·라벨 검수 |
| CONSUMER | 이용기업 사용자 | 승인된 데이터셋 이용 |
| DEVICE | 수집기 (에이전트·엣지) | 매핑된 원천 장비의 업로드·관측값·이벤트, 하트비트, 정책 조회 |
| SERVICE | 내부 서비스 계정 | 파이프라인 처리 |

데이터 범위 (Tenant_Guard는 이 표를 그대로 구현하고, 표에 없는 접근은 거부한다):

| 역할 | 볼 수 있는 메타데이터 | 바이너리 (원본·프록시·다운로드 URL) |
|---|---|---|
| ORG_ADMIN, ORG_MEMBER | 자기 기관 전체 | 자기 기관만. 원본 다운로드는 감사 기록 |
| PROGRAM_VIEWER | 배정 사업의 L1 자산 메타데이터, 제출(SUBMITTED)된 보고서·지표, 수집 정상성 요약 | 받지 않는다 (어떤 등급이든) |
| PLATFORM_OPERATOR | 모든 기관의 메타데이터·상태·정책 (운영 목적, 감사 기록) | 원본은 긴급 열람 승인으로만. 파생본은 심사 중인 대상만 |
| REVIEWER | 배정된 검수 대상 | 배정된 대상의 원본·파생본 프레임만, 모든 열람을 감사 기록 |
| CONSUMER | 게시된 데이터 카드, 자기 이용 신청·권한 | GRANTED 권한의 데이터셋 버전 사본만, 권한 기간 안에서 |
| DEVICE | 자기 수집기·매핑된 원천 장비의 정책·상태 | 업로드 URL만 |
| SERVICE | 처리 작업 대상 | 처리 작업 대상만 |

## 3. 다섯 개의 독립 속성 (하나로 합치지 않는다)
| 필드 | 질문 | 값 | 기본값 |
|---|---|---|---|
| collection_mode | 중앙으로 무엇을 가져오나 | C0_METADATA_ONLY / C1_SAMPLE_PREVIEW / C2_EVENT_CLIPS / C3_APPROVED_FULL | 원천 장비 정책에서 상속 |
| data_role | 기업 경쟁력과 관련 있나 | CORE (성능·알고리즘·튜닝·실패 사례) / BYPRODUCT (환경·배경 영상) | CORE |
| privacy_status | 개인정보 처리 상태 | UNKNOWN / NOT_APPLICABLE / REVIEW_REQUIRED / PSEUDONYMIZED / ANONYMIZED_VERIFIED | REVIEW_REQUIRED |
| sensitivity_class | 보안 민감도 | INTERNAL / RESTRICTED / SECURITY (민감 해역·시설) | RESTRICTED |
| sharing_scope | 누가 볼 수 있나 | PRIVATE(L0) / PROGRAM_REVIEW(L1) / SANDBOX_PARTNERS(L2) / APPROVED_PARTNERS(L3) / PUBLIC(L4) | PRIVATE |

- 화면에는 L0~L4 라벨을, 저장·API에는 코드값을 쓴다.
- privacy_status에는 검수 진행 상태를 넣지 않는다. 진행 상태는 share_status와 review_result가 맡는다.
- sensitivity_class와 privacy_status=NOT_APPLICABLE은 운영자가 근거(점검 기록)와 함께 정하고 감사 로그에 남긴다. ANONYMIZED_VERIFIED는 운영자 승인과 legal_basis_ref가 있어야 한다.
- 파생 자산은 원 자산들의 data_role과 sensitivity_class 중 가장 엄격한 값을 물려받는다 (CORE > BYPRODUCT, SECURITY > RESTRICTED > INTERNAL).
- collection_mode는 공개 등급이 아니다. C3로 들어온 원본도 기본은 L0다. 영상·이미지는 모드별로 받을 수 있는 variant가 정해져 있다 (그 밖의 유형은 C1 이상이면 ORIGINAL을 받는다):

| collection_mode | 센터가 받는 영상·이미지 variant |
|---|---|
| C0_METADATA_ONLY | 없음 (메타데이터와 현장 파일의 크기·sha256만) |
| C1_SAMPLE_PREVIEW | SAMPLE_FRAME, PROXY |
| C2_EVENT_CLIPS | SAMPLE_FRAME, PROXY, CLIP |
| C3_APPROVED_FULL | SAMPLE_FRAME, PROXY, CLIP, ORIGINAL |

허용되지 않은 variant의 업로드는 422 RULE_VIOLATION으로 거부한다.

## 4. 공유 가능 조건 (domain/sharing_rules 한 곳에만 구현)
| 목표 등급 | 조건 (모두 충족) |
|---|---|
| L1 | ORG_ADMIN 승인 |
| L2 | variant ≠ ORIGINAL · data_role=BYPRODUCT · privacy_status ∈ {NOT_APPLICABLE, PSEUDONYMIZED, ANONYMIZED_VERIFIED} · review_result=PASSED · share_status=APPROVED (운영자 최종 승인) · sensitivity_class ≠ SECURITY · legal_basis_ref 존재 · 제공기관 2단 약정 유효 · ORG_ADMIN 승인 |
| L3 | L2 조건 + 데이터셋 버전에 포함 + PLATFORM_OPERATOR 승인 (이용자 약정은 이용 권한을 줄 때 확인) |
| L4 | L2 조건 + privacy_status ∈ {NOT_APPLICABLE, ANONYMIZED_VERIFIED} + sensitivity_class=INTERNAL + PLATFORM_OPERATOR 승인 |

- data_role=CORE(CORE 원본의 파생본 포함)는 L1을 넘을 수 없다. 원본(variant=ORIGINAL)도 L1을 넘지 않는다.
- 공유 심사(share_status)는 원본 단위로 진행한다. 비식별 결과(PSEUDONYMIZED 등)·review_result·L2 이상 등급은 그 원본에서 만든 파생본(DEID, SENSOR_EXPORT 등)에만 붙는다. 원본이 APPROVED가 되면 그 심사에서 만든 파생본에도 share_status=APPROVED와 review_result=PASSED를 기록한다.
- 조건을 충족하지 못하면 충족하지 못한 조건 목록을 돌려준다 (RULE_VIOLATION, `unmet_conditions`).
- 이용 신청의 목적(purpose)은 데이터셋에 포함된 모든 제공기관 2단 약정의 허용 목적 안에 있어야 한다. 아니면 RULE_VIOLATION.
- 데이터셋 버전의 등급은 항목 중 가장 낮은 등급이다. 모든 항목이 L4일 때만 공개(PUBLIC) 데이터셋이다.
- 상향·하향 모두 감사 로그에 사유와 함께 남긴다. L1 이상 결정은 ORG_ADMIN만 할 수 있다.
- 하향은 ORG_ADMIN이 언제든 할 수 있다 (PRIVATE까지). 이미 부여된 이용 권한의 처리(즉시 철회 / 만료까지 유지)를 함께 정한다.
- 보고서·지표를 관리기관에 내는 것은 sharing_scope가 아니라 report_status=SUBMITTED로 한다 (10번).
- `contracts/schemas/asset-metadata.schema.json`의 조건부 규칙은 저장된 상태가 위 조건과 모순되지 않는지 보는 불변식 검사다. 공유 판단 자체는 domain/sharing_rules가 한다.

## 5. 상태 필드 (서로 다른 필드다)
- **ingest_status**: DISCOVERED → UPLOADING → RECEIVED → VALIDATING → REGISTERED → PROXIED → READY_PRIVATE / 예외 QUARANTINED, REJECTED, FAILED
- **share_status**: NONE → CANDIDATE → LEGAL_REVIEW → PROCESSING → QUALITY_REVIEW → APPROVED / 예외 REJECTED, REPROCESS
- **grant_status** (이용 권한): REQUESTED → PROVIDER_APPROVED → GRANTED → EXPIRED 또는 REVOKED / 예외 DENIED
- **report_status** (보고서 버전): DRAFT → APPROVED → SUBMITTED / 새 버전이 승인되면 이전 승인본은 SUPERSEDED
- RECEIVED는 품질 합격이 아니다. 블러 처리 완료는 공유 가능이 아니다.
- QUARANTINED에는 사유 코드(quarantine_reason)가 반드시 붙는다: CHECKSUM_MISMATCH, MISSING_REQUIRED_METADATA, UNKNOWN_SESSION, UNSUPPORTED_FORMAT, CORRUPT_FILE, MIME_MISMATCH, MALWARE_SUSPECTED
- 아래 표에 없는 전이는 도메인 계층에서 거부한다 (409 INVALID_STATE_TRANSITION).

ingest_status (DISCOVERED는 에이전트 로컬 상태이며 서버에 저장하지 않는다)
| 전이 | 주체 | 조건 |
|---|---|---|
| (없음) → UPLOADING | DEVICE, ORG 사용자 | 업로드 세션 생성 |
| (없음) → READY_PRIVATE | DEVICE, ORG 사용자 | C0 메타데이터 전용 등록 (바이너리 없음) |
| READY_PRIVATE → UPLOADING | DEVICE, ORG 사용자 | 바이너리 없는 C0 자산에 허용 variant를 올리기 시작 |
| UPLOADING → RECEIVED | DEVICE, ORG 사용자 | 완료 호출 |
| RECEIVED → VALIDATING | SERVICE | 무결성 검증 시작 |
| VALIDATING → REGISTERED | SERVICE | 해시·형식·필수 메타데이터·회차 통과 |
| VALIDATING → QUARANTINED | SERVICE | 사유 코드와 함께 |
| REGISTERED → PROXIED | SERVICE | 영상의 프록시·썸네일 완료 |
| REGISTERED → READY_PRIVATE | SERVICE | 프록시가 필요 없는 유형 |
| PROXIED → READY_PRIVATE | SERVICE | 후속 기본 처리 완료 |
| REGISTERED, PROXIED → QUARANTINED | SERVICE | 처리 중 손상 발견 (CORRUPT_FILE) |
| QUARANTINED → VALIDATING | ORG 사용자 | UNKNOWN_SESSION·MISSING_REQUIRED_METADATA를 보완한 뒤 재검증 |
| QUARANTINED → REJECTED | ORG_ADMIN, SERVICE | 폐기 확정 또는 격리 보존 기간 만료 |
| 처리 단계 → FAILED | SERVICE | 재시도 소진 |
| FAILED → 실패 직전 단계 | PLATFORM_OPERATOR | 재처리 |

share_status (원본 단위)
| 전이 | 주체 | 조건 |
|---|---|---|
| NONE → CANDIDATE | ORG_ADMIN, ORG_MEMBER | data_role=BYPRODUCT |
| CANDIDATE → LEGAL_REVIEW | PLATFORM_OPERATOR | |
| LEGAL_REVIEW → PROCESSING | PLATFORM_OPERATOR | legal_basis_ref 기록 |
| PROCESSING → QUALITY_REVIEW | SERVICE | 파생본 생성 완료 |
| QUALITY_REVIEW → APPROVED | PLATFORM_OPERATOR | 파생본 review_result=PASSED |
| QUALITY_REVIEW → REPROCESS | REVIEWER, PLATFORM_OPERATOR | 검수 불합격 |
| REPROCESS → PROCESSING | SERVICE | 재처리 등록 |
| CANDIDATE, LEGAL_REVIEW, QUALITY_REVIEW → REJECTED | PLATFORM_OPERATOR | 사유 기록 |
| REJECTED → CANDIDATE | ORG_ADMIN | 보완 후 재신청 |
| 모든 상태 → NONE | ORG_ADMIN | 공유 철회. 파생본은 L1 이하로 내린다 |

grant_status
| 전이 | 주체 | 조건 |
|---|---|---|
| REQUESTED → PROVIDER_APPROVED | 모든 제공기관 ORG_ADMIN | 각 기관 승인 |
| PROVIDER_APPROVED → GRANTED | PLATFORM_OPERATOR | 이용자 약정 확인 |
| REQUESTED, PROVIDER_APPROVED → DENIED | ORG_ADMIN, PLATFORM_OPERATOR, SERVICE | 한 곳이라도 거절, 운영자 거절, 응답 기한 초과 |
| GRANTED → EXPIRED | SERVICE | 기간 만료 |
| GRANTED → REVOKED | ORG_ADMIN, PLATFORM_OPERATOR | 철회 |

report_status
| 전이 | 주체 | 조건 |
|---|---|---|
| DRAFT → APPROVED | ORG_ADMIN | 버전 고정 |
| APPROVED → SUBMITTED | ORG_ADMIN | 근거 손상 표시 없음 |
| APPROVED, SUBMITTED → SUPERSEDED | SERVICE | 같은 보고서의 새 버전 승인 |

## 6. 시간·위치·단위·모델 결과
- 시간은 UTC(ISO 8601/RFC 3339)로 저장하고 화면은 KST로 표시한다.
- **촬영시각을 파일 생성·수정 시각(mtime/ctime)이나 업로드 시각으로 채우지 않는다.** 근거(영상 내부 메타, 장비 로그, GPS, 장비가 쓴 매니페스트)가 없으면 capture_start_at=null, timestamp_quality.source=UNKNOWN, quality_status=WARNING.
- timestamp_quality = `{ source: GPS | NTP | DEVICE | MANIFEST | MANUAL | UNKNOWN, verified: bool, est_offset_ms: int | null }` — MANUAL은 사람이 입력한 시각이다 (기본 verified=false).
- 관측값의 observed_at은 시간대 오프셋이 있는 RFC 3339여야 한다. 오프셋이 없는 원천 시각은 원천 장비 정책에 source_timezone이 있을 때만 변환하고 원래 문자열을 source_timestamp로 보존한다. 둘 다 없으면 그 항목을 거부한다.
- 결측 관측값은 value=null + quality_flag=MISSING이다. 0이나 직전 값으로 채우지 않는다.
- 단위 사전에 없는 단위: 관측값은 quality_flag=SUSPECT, 파일 자산은 quality_status=WARNING.
- 위치는 GeoJSON, 좌표 순서 `[경도, 위도]`, EPSG:4326. 원래 좌표계 값은 source_location으로 보관한다. 위치를 모르면 비운다 ((0, 0)을 넣지 않는다). location_precision = EXACT_RESTRICTED | GENERALIZED | NONE
- 단위는 `contracts/codes/units.json` 단위 사전으로 정규화하고 원래 값과 단위를 함께 보존한다.
- 모델이 만든 결과(태그·이벤트·라벨)는 사람이 확인하기 전까지 human_verified=false이며, 정답 데이터(annotation_kind=GROUND_TRUTH)와 예측(PREDICTION)을 구분한다. 정답 데이터 없이 모델 결과만으로 계산한 성능 지표는 "검증 필요"로 표시하고 확정값으로 쓰지 않는다.

## 7. 멱등성
- 자산 중복 판단 키: (organization_id, source_system_id, checksum_sha256). 파일명으로 판단하지 않는다.
  - 같은 키의 자산에 바이너리가 저장되어 있으면 새 세션을 만들지 않고 ALREADY_REGISTERED와 기존 asset_id를 돌려준다.
  - 같은 키의 자산이 바이너리 없는 C0 자산이면, 정책이 허용할 때 그 asset_id로 업로드 세션을 연다.
- 관측값 중복 판단 키: (organization_id, source_system_id, metric, observed_at, depth_m, channel). 없는 값은 빈 값으로 비교한다. 같은 키·같은 값은 DUPLICATE, 같은 키·다른 값은 덮어쓰지 않고 VALUE_CONFLICT로 거부한다.
- 이벤트: 원천 이벤트 ID가 있으면 (organization_id, source_system_id, source_event_id)로 중복을 막는다.
- 쓰기 API는 `Idempotency-Key` 헤더를 받는다 (기본 24시간 보관). 키의 범위는 (organization_id, 자격증명 주체, 메서드+경로)다. 같은 키·같은 본문은 처음 응답을 그대로 돌려주고, 같은 키·다른 본문은 409 IDEMPOTENCY_CONFLICT.

## 8. 공통 메타데이터 v1.0
schema_version, asset_id, dataset_id?, organization_id, project_id, session_id?, source_system_id,
data_type(VIDEO | IMAGE | SENSOR | EVENT | AIS | LOG | DOCUMENT | ANNOTATION), variant(ORIGINAL | PROXY | THUMBNAIL | SAMPLE_FRAME | CLIP | DEID | LABEL | SENSOR_EXPORT), data_role,
capture_start_at?, capture_end_at?, ingested_at, timestamp_quality, location?, source_location?, area_code?, location_precision?,
mime_type, byte_size, checksum_sha256, has_binary, collection_mode, privacy_status, sensitivity_class, sharing_scope,
legal_basis_ref?, retention_policy_id, retention_until, quality_status(PENDING | PASSED | WARNING | FAILED),
ingest_status, quarantine_reason?, share_status, review_result?(PASSED | FAILED), backup_class(NONE | PROTECTED),
provenance, env_context?, tags?,
확장: video_ext, cctv_ext, vessel_ext, sensor_ext, external_obs_ext, annotation_ext, evidence_ext

- 기업이 직접 입력하는 필수 필드는 없다. 에이전트 설정·파일 내부 정보·토큰·파이프라인이 채운다.
- organization_id는 인증 정보로만 정한다. 요청 본문으로 받지 않는다.
- session_id가 비어 있는 자산은 등록 상태가 될 수 없다 (회차 미상은 QUARANTINED + UNKNOWN_SESSION).
- C0_METADATA_ONLY 자산은 has_binary=false이며 현장 파일의 byte_size와 checksum_sha256을 가진다.
- 기계가 읽는 정의는 `contracts/schemas/`, 필드별 설명은 `docs/baseline-v0.04.md` 7장 (`#baseline-reference`).

## 9. 저장 존·쿼터·백업
| 존 | 버킷 (설정값) | 내용 |
|---|---|---|
| RAW | raw | 원본 (L0) |
| PROCESSED | proc | 프록시·썸네일·샘플 프레임·클립·비식별본·센서 내보내기 |
| EVIDENCE | evidence | 보고서·증빙 산출물 |
| DATASET | datasets | 데이터셋 버전 사본 (수정 불가) |
| QUARANTINE | quarantine | 격리 |

- 객체 키: `{organization_id}/{project_id}/{session_id}/{source_system_id}/{asset_id}/{variant}` (회차 미상 격리 자산은 session_id 자리에 `_unassigned`)
- 원래 파일명은 메타데이터로만 보관하고 객체 키에 넣지 않는다.
- 격리 보존: CHECKSUM_MISMATCH·UNSUPPORTED_FORMAT·CORRUPT_FILE·MIME_MISMATCH·MALWARE_SUSPECTED는 기본 30일 뒤 삭제한다. UNKNOWN_SESSION·MISSING_REQUIRED_METADATA는 손상 없는 원본일 수 있으므로 자동 삭제하지 않고 ORG_ADMIN이 폐기를 확정할 때만 지운다.
- 기관 사용량 = RAW + PROCESSED + EVIDENCE + QUARANTINE의 그 기관 객체 + 진행 중 업로드 예약량 + 기관이 신청한 백업 용량. 화면에는 항목별로 나눠 보여준다. 격리되면 예약량은 사용량으로 바뀌고, 격리 객체가 지워질 때 줄어든다.
- 스토리지 자체 보호(복제·이레이저 코딩)는 백업이 아니다.
- 원본(RAW)은 기본적으로 독립 백업하지 않는다 (backup_class=NONE). 원천 장비마다 현장 사본 정책 local_copy_policy(RETAINED | ROTATING | NONE | UNKNOWN)를 연계 카드에서 받아 기록한다. 에이전트가 감시하는 폴더 원본은 RETAINED, 엣지 박스·NVR 순환 녹화는 ROTATING이다.
- 다음 자산은 backup_class=PROTECTED로 올려 독립 백업한다:
  1. 승인된 증빙·보고서가 인용한 자산 (클립을 인용했으면 그 클립)
  2. local_copy_policy가 ROTATING·NONE·UNKNOWN인 원천 장비의 자산 중 기관이 백업을 신청한 것 (백업 용량은 그 기관 사용량에 들어간다)
- local_copy_policy가 ROTATING·NONE·UNKNOWN인 원천 장비는 장비 화면과 약정 안내에 "센터 원본이 유일본일 수 있음"을 표시한다.
- 에이전트는 감시 폴더의 원본을 지우지 않는다. 예외는 하나다: 캡처 플러그인(15번)이 스스로 만든 파일은 원천 장비가 ROTATING일 때만, 영수증을 받았거나 현장 보존 기간이 지난 뒤 지울 수 있고, 지운 내역을 하트비트로 보고한다.

## 10. 수용시험 ID (tests/acceptance/)
| ID | 주제 | ID | 주제 |
|---|---|---|---|
| T01 | 테넌트 격리 | T11 | 보고서 (실제 양식, HWPX 포함) |
| T02 | 자동수집 (폴더 에이전트) | T12 | 성능·동시성·피크 |
| T03 | 대용량 재개 | T13 | API 문서만으로 외부 연계 |
| T04 | 무결성·격리 | T14 | 운영 인수 (Runbook) |
| T05 | 스키마·정규화 (촬영시각 미상 포함) | T15 | 보존·삭제·철회 |
| T06 | 계보 역추적 | T16 | GPU/CPU 처리량·우선순위 |
| T07 | 수집 정상성 경보 | T17 | 환경 정보 결합 |
| T08 | 공유 조건 강제 | T18 | 확장성 (노드·디스크 추가) — 하드웨어 후 |
| T09 | 비식별 검수 절차 | T19 | 조건부 표준 연계 (NGSI-LD·광역 허브) |
| T10 | 백업·복원 | T20 | 구축비·운영비 구분 — 문서 시험 |

세부 시나리오(T01-a 등)와 담당 스펙은 `docs/ACCEPTANCE.md`에 있다. 시험 코드는 같은 ID로 이름을 붙인다.
