# 작업: 01 개발 기반

표시: [x] 완료(코드+시험) · [ ] 남음

- [x] 1. 저장소 골격·pyproject·린트 설정 — 요구사항 7.1, 9.1
- [x] 2. 설정 로더와 누락 키 보고 — 요구사항 2.1, 2.2, 2.3 · 시험 `test_missing_settings_report_only_key_names`
- [x] 3. JSON 로그·마스킹·쿼리 제거 — 요구사항 4.1, 4.3, 4.4 · 시험 `test_strip_query_and_scrub`, `test_json_log_*`
- [x] 4. 요청 ID 미들웨어 — 요구사항 4.2 · 시험 `test_healthz_and_request_id`
- [x] 5. RFC 9457 오류 처리 — 요구사항 10.1~10.3 · 시험 `test_problem_format_*`, `test_validation_error_has_json_pointers`
- [x] 6. /healthz·/readyz — 요구사항 3.1~3.3 · 시험 `test_readyz_reports_failed_dependency_names_only`
- [x] 7. /metrics, 워커 메트릭 — 요구사항 8.1, 8.2 · 시험 `test_metrics_exposed`
- [x] 8. 포트 8종 + 개발 어댑터 5종 + 미구현 골격 3종 — 요구사항 7.1, 7.2
- [x] 9. domain import 검사 — 요구사항 7.3 · 시험 `test_domain_layer_has_no_framework_imports`
- [x] 10. Alembic 0001 (down 포함) — 요구사항 5.1, 5.2 · CI에서 up→down→up
- [x] 11. Compose·make up·버킷 생성 — 요구사항 1.1, 1.2, 1.4, 1.5 (2026-11-04 클라우드 환경에서 전체 healthy 확인)
- [x] 12. make up 실패 경로 시험 — 요구사항 1.2, 1.3 · `tools/tests/test_compose_up.py`(가짜 docker로 헬스체크 실패 서비스 이름 출력·비0 종료, 정상 종료한 migrate 제외, .env 없음 2, 모두 정상 0). CI `python -m unittest discover -s tools/tests`에서 실행
- [x] 13. 시험 데이터·반복 실행 — 요구사항 6.1, 6.2
- [x] 14. 합성 영상·대용량 파일 — 요구사항 6.3~6.5 (`make sample-video`, `LARGE_GB=10`)
- [x] 15. /v1/system/capabilities — 요구사항 11.1, 11.2 · 시험 `test_capabilities_requires_auth_and_hides_settings`
- [x] 16. CI 파이프라인 — 요구사항 9.1~9.5 (Go 린트·gosec은 06번에서 agent/ 생기면 활성)
- [x] 17. golangci-lint 설정 — 요구사항 9.1 · `agent/.golangci.yml`(v2 형식, standard 린터 errcheck·govet·ineffassign·staticcheck·unused + gofmt, 제외 규칙 없음). v2.15.0을 `go run`으로 실행(GPL-3.0 개발 도구라 go.mod에 넣지 않음). CI agent 작업·`make agent-lint`에서 Linux와 `GOOS=windows`로 두 번 검사. 처음 지적 43건(Windows 전용 2건 포함)을 모두 코드에서 처리하고 설정 제외·nolint는 0건: 쓰기 파일 닫기 오류 확인·반환 4건(진단 zip, 워커 로그·상태 DB, 감독 로그), 버리던 채널 오류 기록 3건, 설정 임시 파일을 닫은 뒤 지우도록 순서 수정 2건, staticcheck 3건·미사용 함수 1건 수정, 읽기 전용 닫기·응답 본문·rows.Close·Commit 뒤 Rollback·임시 파일 삭제·LocalFree 30건은 `_ =`로 명시
