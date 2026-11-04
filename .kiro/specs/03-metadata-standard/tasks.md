# 작업: 03 메타데이터 표준

- [x] 1. 마이그레이션 0002 (자산·계보·정책·약정·연계 카드·코드·품질 규칙·관측값 파티션, down 포함) — 요구사항 1.1~1.5, 9.1, 12.1~12.3
- [x] 2. 기본값·variant 허용·엄격값 상속 — 요구사항 3.1, 3.2 · 시험 `test_defaults_*`, `test_variant_allowed_*`, `test_inherit_strictest`
- [x] 3. schema_version 확인 순서 — 요구사항 4.3 · 시험 `test_schema_version_rule`
- [x] 4. 스키마·도메인 검증기 + 예시 41건 판정 일치 — 요구사항 4.1, 4.2, 13.1, 13.2 · 시험 `test_validate_endpoint_uses_same_rules`
- [x] 5. 촬영시각 규칙 — 요구사항 5.1~5.4 · 시험 `test_capture_time_*`
- [x] 6. 위치 범위·뒤바뀜 검사(속성 기반)·정밀도 낮추기 — 요구사항 6.1, 6.2, 6.4 · 시험 `test_swapped_lonlat_detected`, `test_generalize_location`
- [ ] 7. 원래 좌표계 → EPSG:4326 변환 (PostGIS ST_Transform) + 변환 이력 보관 — 요구사항 6.3 (04번 업로드 등록 때 연결)
- [x] 8. 단위 사전 정규화·원래 값 보존·미등록 단위 — 요구사항 7.1, 7.2 (관측값 SUSPECT 저장은 07번)
- [x] 9. 스키마 게시·번들 일치 시험 — 요구사항 2.1, 2.2 · 시험 `test_schema_index_and_bundle_match_repository_files`
- [ ] 10. 계약 → 타입 생성 (datamodel-code-generator·openapi-typescript) — 요구사항 2.3 (현재는 계약 시험으로 검증)
- [x] 11. 코드표 API (+ 기관 하위코드 조회) — 요구사항 8.1, 8.2
- [ ] 12. 기관 하위 이벤트 코드 추가 API (ORG_ADMIN), 상위분류는 운영자만 — 요구사항 8.3 (contracts 먼저)
- [x] 13. 품질 평가 함수 — 요구사항 10.1 · 시험 `test_quality_evaluator_rules`
- [ ] 14. 품질 규칙 DB 로딩 + 운영자 재평가 명령 — 요구사항 10.2
- [x] 15. 계보 조회(상·하위, 범위 필터) — 요구사항 11.2 · 시험 `test_lineage_and_asset_scope` (기록은 05번 파생 생성 때)
- [x] 16. 장비 정책 조회 (저장본 없으면 장비 표로 기본 정책) — 요구사항 1.3 일부
- [ ] 17. 약정·연계 카드 등록 API — 요구사항 1.4, 1.5 (화면은 09번, contracts 먼저)
