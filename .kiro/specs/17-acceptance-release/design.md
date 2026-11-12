# 설계: 17 수용시험·운영 인수·릴리스 게이트

## 이번에 구현한 것
| 구성 | 구현 |
|---|---|
| Acceptance_Runner | 시험 함수에 `@pytest.mark.covers("T07-a", …)`. `ACCEPTANCE_RESULTS`가 있으면 conftest 훅이 covers 시험 결과를 JSON Lines로 기록 → `tools/acceptance_report.py`가 docs/ACCEPTANCE.md 세부 ID별로 PASS·FAIL·NOT_RUN(전부 건너뜀), 시험이 없으면 `backend/tests/acceptance/status.json`의 NEEDS_HARDWARE·NEEDS_INPUT·NOT_IMPLEMENTED(사유)로 보고. 실패에는 재현 명령·로그. Go 에이전트 시험은 함수 위 `// covers: T02-a` 주석 → `tools/go_acceptance.py`가 `go test -json`을 같은 JSON Lines로 변환 (재현 명령 `go test -run`). `make test-acceptance` → `var/acceptance.json`·`.md` (2026-11-04 사본: docs/ACCEPTANCE-STATUS.md — PASS 65, NOT_IMPLEMENTED 12, NEEDS_INPUT 5, NEEDS_HARDWARE 2) |
| 가상 3사 여정 (T06-d) | `test_t06d_journey.py`: A 선박 영상(실제 업로드 API·S3·무결성 검증·메타 추출)·B CCTV(합성 비식별본)·C 해양 센서(새 합성 기관, 관측값 API) + 이용기관. 회차 기록·보고서(C는 관측 통계 지표) → 공유 후보·심사·비식별·검수·승인 → 데이터셋(매니페스트 계보: 비식별본 → 원본) → 두 제공기관·운영자 승인 → 배포 파일 sha256 → 이용 이력 |
| 성능·동시성 | `test_t12_concurrency.py`: 동시 업로드 12건·쿼터 5건분 → 정확히 5건만 201, 예약+사용 ≤ 쿼터 (T12-a). 워커 없이 업로드 완료(RECEIVED)·관측값 수신, 처리 작업은 대기열에 (T12-c). `make bench` 단계별 처리량 JSON (T16-b). Locust 시나리오 `loadtest/`(업로드 세션·파트·완료, 관측값 배치, 자산 검색 — 사용자 수·시간·크기 설정, 결과 JSON, 합격 수치 없음) |
| 보안 시험 | `.github/workflows/security.yml`: API 이미지 SBOM(Syft, SPDX) → `tools/check_sbom_licenses.py`(pypi·npm·golang 패키지를 허용 목록·예외로), Trivy 이미지·파일시스템(높음·심각, 고칠 수 있는 것), OWASP ZAP baseline(주간·수동, 보고서 산출물). 교차 기관 접근은 T01 수용시험. 로컬: `make sbom`, `make vuln-scan` |
| Runbook | `docs/runbook/` 10개(기동·중지, 배포, 백업·복원(초안), 자격증명 폐기, 긴급 열람(초안), 스토리지 장애, 작업 큐, 용량 임계, 토큰 유출, 데이터셋 철회) — 판단 기준·자동 대응·사람이 할 것·완료 확인. 수행 기록 양식 `records/TEMPLATE.md` (T14) |
| 릴리스 게이트 | `tools/release_gate.py`(`make release-gate MILESTONE=M1 BASE_REF=<태그>`): 계약·추적성·비밀값·라이선스(런타임, SBOM 있으면 SBOM)·시험과 수용시험(시험 DB가 있으면)·마일스톤 세부 케이스 PASS·웹 접근성 검사(`web_a11y` = `npm run test:a11y`, 웹 빌드 뒤, 09번 요구사항 11.2·ADR-0039)·계약 주버전 변경 시 `info.x-compatibility`(overlap_until·previous_end_of_support) 필수. 결과 `var/release-gate.json`. `make release-notes`: 포함 스펙·계약 변경(git diff)·통과 수용시험·알려진 제한(tasks.md 미완료) |

## 결정
- 수용 보고의 단위는 docs/ACCEPTANCE.md 세부 ID. 시험이 실제로 확인하는 것만 covers로 단다 (짝 맞추기용 표시 금지, AGENTS.md 8)
- 마일스톤별 필수 세부 케이스 목록은 `release_gate.py`의 `MILESTONE_CASES`(docs/ROADMAP.md 2장에서 옮김). M1은 기능 범위 확인용 부분 집합, M2는 운영 하드웨어 실측

## 확인 필요
- 기존 GitHub Actions 실패는 확인했고 로컬에서 보완했다. 변경 후 원격 CI 결과는 아직 없으며 릴리스 라이선스 확인·승인이 필요하다
- 웹 E2E(T02-g)는 후속 보완에서 공통 JSON Lines로 연결했다. 실행 환경이 없으면 미실행/실패로 보고한다
- T14 운영 인수(스테이징 Runbook 수행 기록), T12-b·T18 실측은 하드웨어 후

## 로컬 후속 보완 (2026-11-05, ADR-0023)
- `tools/run_acceptance.py`가 Python·Go·웹 시험을 실행하고 항상 수용 보고를 만든다. 실행기·수집 실패도 exit 1이다.
- T02-g는 별도 Compose 프로젝트의 실제 제품 API·DB·S3 업로드를 사용한다. 시험 인증은 합성 IdP 어댑터 안에만 둔다.
- `getChunkSize`가 Blob을 받는 Uppy 동작에 맞춰 서버 part_size를 Blob별로 연결한다. 3파트 실제 재개·해시 시험으로 검증했다.
- 라이선스 파서는 SPDX 논리를 보존하고 PyPI 이름을 정규화한다. LicenseRef는 추출 원문만 해석한다.
  개발 검사의 경고와 strict 릴리스 차단을 분리하며, 허용 목록·사람 승인 상태는 유지한다.
- 게이트는 현재 npm ci·이미지/SBOM·모든 시험·임시 DB 왕복·보안 검사를 요구한다. 누락·생략은 실패다.
  API·웹 ZAP 결과는 위험 등급까지 검사한다. 보안 CI는 라이선스 실패가 있어도 취약점 보고서를 남긴다.
- 계약·도메인 코드값·DB 구조 변경 없음. 새 마이그레이션(down 포함)은 해당 없음이다.
- 확인 필요는 원격 CI 재실행, 미상 라이선스 원문 확인·사람 승인, 스테이징 Runbook과 하드웨어 실측이다.
