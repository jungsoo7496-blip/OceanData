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
- 정규 위치 없이 원래 좌표가 들어오면 수집 서비스가 PostGIS `ST_Transform`으로 EPSG:4326으로 바꾼다(6.3).
  결과에 공통 GeoJSON 스키마와 위치 규칙을 모두 적용한다. 원본은 `source_location`에, 변환 방식·원래/대상 좌표계는 내부 `ingest_context.location_transform`에 저장한다.
  검증된 `location`이 함께 전달되면 기존대로 이를 사용하고 원래 좌표를 보존한다. 신규 DB 마이그레이션/down은 해당 없다.
- `domain/quality.py`: 규칙 기반 quality_status·사유

## 서비스
- `services/contracts.py`: 스키마 레지스트리·번들(common 참조를 `$defs`로 인라인), 코드표, `validate_document()` — 사전 검증 API와 04번 업로드가 같은 함수를 쓴다 (13.2)

## API
`GET /v1/schemas`, `GET /v1/schemas/{name}/{version}`(application/schema+json), `GET /v1/codes/{table}`, `POST /v1/contracts/validate`, `GET /v1/devices/{id}/policy`, `GET /v1/assets/{id}/lineage`(재귀, 권한 범위 밖 자산 제외)

`POST /v1/codes/event-categories/subcodes`는 사람 `ORG_ADMIN`의 인증 기관에 `code`와 `label`을 등록한다(8.3, ADR-0026).
기존 0002의 `org_event_codes`를 사용하며 신규 마이그레이션/down은 해당 없다. 기관 내 중복은 409, 미등록 상위분류는 422다.
등록한 코드는 자기 기관의 코드표 조회와 이벤트 제출에 쓸 수 있다. 공통 상위분류와 버전은 공통 코드표에서 관리한다.
같은 멱등성 키의 요청을 직렬화하고 등록·멱등성 응답·성공 감사를 한 트랜잭션으로 저장한다.

## Tenant_Guard 변경
PROGRAM_VIEWER는 자산 테이블에서 배정 사업 + sharing_scope ≠ PRIVATE만 본다 (domain.md 2장 "L1 자산 메타데이터").

## 품질 규칙 재평가

운영 환경에서 DB 접근 권한이 있는 운영자가 아래 명령을 실행한다(10.2, ADR-0027).

```bash
python -m app.cli reevaluate-quality "$ORG_ID"
python -m app.cli reevaluate-quality "$ORG_ID" --asset-id "$ASSET_ID"
```

활성 `quality_rule_sets` 문서 예시는 `{"version":2,"required_fields":{"VIDEO":["capture_start_at"]}}`이다.
DB 버전과 문서 버전을 맞추고 한 행만 활성으로 둔다. 지원하는 필수 필드는 데이터 유형, MIME·크기·해시,
시각 품질·시작/종료 시각, 회차, 위치, 원천 장비다. 활성 규칙이 없으면 기본 규칙을 사용한다.

명령은 `rule_version`, `evaluated`, `changed`, `skipped_missing_facts`를 출력하고 기관별 실행을 감사 로그에 남긴다.
선택 대상은 삭제되지 않은 REGISTERED·PROXIED·READY_PRIVATE 자산이다. 재평가로 공유·격리·저장 위치를 변경하지 않는다.
원래 범위 경고를 입증할 값이 없는 자산은 그대로 두고 건수를 보고한다. 공개 API와 DB 구조는 변경하지 않는다.

## 품질 집계 (작업 18)

`GET /v1/quality-reports`(10.1, STD-03)는 원본(variant=ORIGINAL)이면서 삭제되지 않은 자산의 quality_status별 수와
사유가 붙은 자산 수 상위 10개(같으면 사유 이름순)를 돌려준다. 네 상태는 0이라도 모두 넣는다.
범위는 `scope_filter`대로 제공기관(사람·status:read 기관 API 키)은 자기 기관, 운영자는 전체다. 관리기관 열람자·검수자·이용기관은 403.
`session_id`가 보이지 않으면 404, 기간은 ingested_at 기준 [from, to)이고 from ≥ to면 400이다. 파생본은 원 자산의 품질을
물려받지 않고 PASSED로 만들어지므로 세지 않는다. 신규 마이그레이션은 없다.

## 약정·연계 카드 (작업 17, ADR-0028)
- 약정: `GET /v1/agreements`(운영자 전체·제공기관 자기 기관), `POST /v1/agreements`(운영자·다중 인증, TIER1 목적 없음·TIER2 목적 1개 이상·
  purpose 코드표·시간대 필수), `POST /v1/agreements/{id}/end`(사유, valid_until을 당김 — 새 상태 코드 없음, 13번 `tier2_valid()`에 바로 반영).
- 연계 카드: `contracts/schemas/integration-card.schema.json`(부록 A 15개 분야, 분야 안은 자유 기록, `collection_mode_requested`·
  `current_storage.local_copy_policy`만 domain.md 코드 검사, 담당자는 사용자 ID·역할만). ORG_ADMIN이 자기 기관 카드 등록·새 버전,
  장비 지정 시 local_copy_policy를 장비에 반영(감사), 희망 collection_mode는 기록만. 다른 기관 카드·장비는 404 + 교차 기관 감사.
- 정책 연결: `python -m app.cli set-policy <수집기> <정책.json> --integration-card <icd_…>` → `device_policies.integration_card_id`,
  카드 응답 `linked_policies`. 0002 표 사용으로 신규 마이그레이션 없음.

