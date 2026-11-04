# 설계: 03 메타데이터 표준

## 데이터 모델 (마이그레이션 0002)
| 테이블 | 내용 | 요구사항 |
|---|---|---|
| assets | 공통 메타데이터 v1.0 컬럼 + 확장(ext JSONB) + `location geometry(4326)`(GIST) + `embedding vector`(차원은 14번) + 저장 위치(storage_zone·storage_key, API로 노출 안 함). 유일 제약 (organization_id, source_system_id, checksum_sha256) | 1.1, 1.2, 9.1, 12.2, 12.3 |
| asset_lineage | 파생 → 원 자산, relation·tool_version·model_ref·run_by·시각 | 11.1 |
| device_policies | 수집기 정책 문서(device-policy.schema.json) 버전별 | 1.3 |
| agreements | 1단(TIER1)·2단(TIER2) 약정, 참조·유효기간·허용 목적 | 1.4 |
| integration_cards | 연계 카드 버전별, 장비 정책과 연결 | 1.5 |
| org_event_codes | 기관 하위 이벤트 코드 | 8.3 |
| quality_rule_sets | 품질 규칙 버전 (활성 1개) | 10.1 |
| observations | 월 단위 RANGE 파티션(2026-01~2028-12 + DEFAULT), `UNIQUE NULLS NOT DISTINCT` 중복 키, `ensure_observation_partition()` | 12.1 |

ID 접두사 추가: `agr_`(약정), `icd_`(연계 카드), `brk_`(긴급 열람) — structure.md·common.schema.json 반영.

## 도메인 (순수 함수)
- `domain/metadata.py`: 기본값(3.1), collection_mode별 variant 허용표, 파생 자산 엄격값 상속, schema_version 규칙(4.3)
- `domain/normalize.py`: 촬영시각(근거 없으면 UNKNOWN·비움, 오프셋→UTC·원문 보존, mtime 무시), 위치(범위·위경도 뒤바뀜·(0,0) 거부, 정밀도 낮추기), 단위 사전 정규화
- `domain/quality.py`: 규칙 기반 quality_status·사유

## 서비스
- `services/contracts.py`: 스키마 레지스트리·번들(common 참조를 `$defs`로 인라인), 코드표, `validate_document()` — 사전 검증 API와 04번 업로드가 같은 함수를 쓴다 (13.2)

## API
`GET /v1/schemas`, `GET /v1/schemas/{name}/{version}`(application/schema+json), `GET /v1/codes/{table}`, `POST /v1/contracts/validate`, `GET /v1/devices/{id}/policy`, `GET /v1/assets/{id}/lineage`(재귀, 권한 범위 밖 자산 제외)

## Tenant_Guard 변경
PROGRAM_VIEWER는 자산 테이블에서 배정 사업 + sharing_scope ≠ PRIVATE만 본다 (domain.md 2장 "L1 자산 메타데이터").
