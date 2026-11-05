# 인수인계 (클라우드 → 로컬)

이 파일은 클라우드 세션에서 만든 작업을 로컬에서 이어가기 위한 안내다. 작업이 진행될 때마다 갱신한다.
최종 갱신: 2026-11-05 (클라우드 Claude — 08번 삭제 보호 보완. 그 전: 로컬 Codex 03·06·08·17번 보완)

## 0. 남은 일 (우선순위 순) — 로컬에서 여기부터
**로컬에서 확인·수정한 것과 다음 우선순위**
-1. **08번 삭제 보호 보완 (2026-11-05, 클라우드):** 유효한 이용 권한(GRANTED)이 있는 데이터셋 버전에 든 자산이 `confirm_affected=true`로 삭제되던 빈틈을 막았다 — 재확인과 무관하게 409·철회 안내(원본 삭제로 파생본에 닿는 경로 포함), 철회 뒤에는 삭제 가능. 증빙·보고서 인용은 그대로 재확인 후 삭제 + 근거 손상(DELETED) 표시를 API 시험으로 확인. 계약 설명 갱신, 수정 전 코드에서 새 시험이 실패(200)하는 것 확인. 백엔드 전체 226건 통과, 계약·Go SDK 일치 검사 통과. 새 마이그레이션 없음. **다음: 03번 작업 17 약정·연계 카드 등록 API.**
-1b. **03번 작업 17 약정·연계 카드 등록 API 완료 (2026-11-05, 클라우드, ADR-0028):** `GET/POST /v1/agreements`(운영자·다중 인증, 종료는 `/end`로 valid_until을 당김 — 새 상태 코드 없음, 13번 공유 규칙에 바로 반영), `GET/POST /v1/integration-cards`·`/{id}`·`/{id}/versions`(ORG_ADMIN, 부록 A 분야별 기록, 정책 코드만 검사, 장비 local_copy_policy 반영·감사), `set-policy --integration-card`로 장비 정책 연결. 계약 먼저(스키마·예시·실패 예시), Go SDK 재생성 일치. 새 마이그레이션 없음(0002 표). **실제 연계 카드 양식은 확인 필요.**
-1c. **02번 긴급 열람 완료 (2026-11-05, 클라우드, ADR-0029):** `/v1/break-glass-grants`(요청·`/decision`·`/revoke`·조회), 다른 운영자 승인(설정으로 본인), 대상 기관 ORG_ADMIN 메일, 요청 운영자만 download-url 허용 + `BREAK_GLASS_ACCESS` 감사, 만료 즉시 차단·워커 EXPIRED. Runbook 05 갱신. 새 마이그레이션 없음.
-1d. **04번 웹훅 완료 (2026-11-05, 클라우드, ADR-0030):** 구독 API(https·사설 주소 거부·비밀 한 번), 상태 변경 같은 트랜잭션의 outbox(0015, down 포함), 워커 서명 전송·지수 백오프·N회 실패 시 구독 끔+기관 메일, 전송 직전 DNS 사설 주소 차단. 남은 위험: DNS 재바인딩은 나가는 방화벽으로(확인 필요).
-1e. **04번 악성코드 검사 포트 완료 (2026-11-05, 클라우드, ADR-0031):** 무결성 검증 스트리밍 중 검사, 의심 시 QUARANTINED(MALWARE_SUSPECTED), 기관 정책 `org-policy --malware-scan on`(0016, down 포함), 검사기 없으면 상태 전이 전에 멈춰 재시도. `MALWARE_SCANNER=mock-eicar`는 시험용 — **실제 검사기 제품은 결정 대기**.
0. **03번 작업 7·12·14 완료. 다음은 같은 스펙의 작업 17 약정·연계 카드 등록 API다. 기존 CLI·테이블·권한을 읽고 계약부터 진행한다.** 업로드 원래 좌표의 EPSG:4326 변환 결과에 공통 GeoJSON·좌표 검사를 추가했다. 계약 선행 검사, PostGIS 관련 통합·응답 계약 6개, 생성 Go SDK 일치, Ruff·Mypy 통과. 원본·변환 이력 보존과 무효 변환의 400/미생성을 확인했다. 새 DB 마이그레이션/down은 해당 없음. `var/contracts-03-task7.log`, `var/meta03-location-integration.log`, `var/agent-codegen-03-coordinates.log`.

   **품질 재평가:** `python -m app.cli reevaluate-quality <기관ID> [--asset-id <자산ID>]`는 활성 DB 규칙을 사용하고 격리·삭제·미완료 자산과 관측 근거가 없는 과거 범위 경고를 보호한다. 신규 PostgreSQL 시험 6개, 수집·미디어 회귀 5개, Ruff·Mypy 통과. 잘못된 규칙으로 RECEIVED를 막지 않고 정상화 뒤 재시도했다. 공개 계약 및 DB 구조 변경 없음. `var/meta03-quality-integration-final.log`, `var/meta03-quality-regression.log`(ADR-0027).

   **기관 하위 이벤트 코드 등록:** `POST /v1/codes/event-categories/subcodes`는 ORG_ADMIN이 자기 기관에 하위코드를 등록하며 기존 코드표 조회와 이벤트 제출에 바로 연결된다. 계약을 먼저 갱신해 `make contracts-check`(126 작업) 및 SDK 재생성 일치 검사를 통과한 뒤 구현했다. 신규 5개 포함 PostgreSQL 통합·계약 13개, 기존 감사 3개, Ruff·Mypy 통과. 감사·등록·멱등성은 한 트랜잭션으로 저장한다. 0002 테이블 재사용으로 새 마이그레이션/down은 해당 없음. `var/contracts-03-task12.log`, `var/meta03-integration-final.log`, `var/meta03-audit-regression.log`(ADR-0026).

   **06번 Windows 서명 업데이트 작업 15·16 로컬 완료.** Windows 자식에 별도 콘솔을 생성하지 않도록 했고 워커 준비와 이전 버전의 재기동을 실제로 확인하는 시험으로 보강했다. Windows native 업데이트 14개가 28.1초에 통과(`var/update-no-console-windows-native.log`), 최종 Windows 제품 CLI 서명 갱신 1.0.0 → 2.0.0 → 롤백 1.0.0이 66.1초에 통과(`var/update-cli-nowindow-20261105-151357-30068/result.json`). 양쪽 바이너리 해시와 DPAPI·원본 불변을 확인했다. Linux 업데이트 15개 race 및 Linux/Windows 대상 vet도 통과했다. 이전 실패·CreateProcess 스택과 프로브를 보존했다(`var/update-windows-native.log`, `var/update-ready-windows-native.log`, `var/update-createprocess-probe.jsonl`). 콘솔 없이 실행한 제품 CLI에서도 초기화 지연이 한 차례 관찰됐으므로 OS 지연 원인은 확인 필요다. 실제 서비스 설치·로그온 계정·부팅과 15번 ROTATING은 별도다. 자동 갱신은 기본 꺼짐이다. API·도메인·서버 DB/SQLite 스키마 변경이 없어 신규 마이그레이션/down 해당 없음(ADR-0024).
1. 2026-11-04 기준선 전체 시험 재실행 완료: 백엔드 고유 시험 192개(단위 100, 일반 통합·계약 60, 수용·covers 56은 일부 중복), Go 19개(MQTT 포함) 통과. 마이그레이션 14개 up/down 왕복, 계약·추적성·Ruff·Mypy·Bandit 통과. Windows 호스트에서 독립 Docker 시험 환경 사용, `make` 대상과 같은 시험 선택식으로 실행.
2. **17번 로컬 보완 완료(ADR-0023):** Router 7.18.5·Vite 6.4.5 등으로 npm 런타임 취약점 3 HIGH → 0건, 전체 HIGH/CRITICAL 0건(개발 MODERATE 11건 남음). PyPI 이름·SPDX·LicenseRef 검사를 고치고 strict에서 미상/미승인을 차단한다. Trivy 이미지·소스 HIGH/CRITICAL 0건, API·빌드 웹 ZAP HIGH 0건(경고 각각 9·10종). 필수 검사 누락·과거 결과·실행기 실패로 릴리스가 통과하는 길을 막았다. 새 마이그레이션 도구의 임시 DB 왕복도 실측 통과. 기존 main의 [ci](https://github.com/jungsoo7496-blip/OceanData/actions/runs/41819956508)·[security](https://github.com/jungsoo7496-blip/OceanData/actions/runs/41819956036) 실패 원인을 보완했으나 **변경 후 원격 CI·전체 릴리스 성공으로 보고하지 않는다**. 라이선스 미상 9종/미승인 예외 3종 등으로 strict 릴리스는 차단 중이다. 06번 후속 검증은 위 0번에 기록했다.
3. **08번 백업 실행 환경 보완 완료(ADR-0022):** 이미지에 PGDG 클라이언트 16, Compose 백업 설정과 `migrate`·`worker` 소유자 연결 전달, 도구·서버 주버전 검사, T10 오류 전체 출력. PG 도구 16.15/서버 16.10에서 T10 통과. 최종 비루트 이미지로 실제 CLI 백업 `SUCCEEDED`·복원 `ok: true`(106개 표·감사 체인 확인). API·도메인 코드값·DB 구조 변경 없음, 새 마이그레이션·down 해당 없음.
4. 라이선스 예외 승인 대기: ADR-0011(PSF-2.0), ADR-0018(Pillow·Pyphen) — 사람 승인.

**완료한 웹 재개와 다음 미구현 작업**
5. **T02-g 완료:** 실제 탭 종료 → 동일 upload_id/asset_id → 저장된 파트 건너뛰기 → 전체 해시/크기 → READY_PRIVATE 통과. 시험이 찾아낸 Uppy Blob 파트 크기 오류 수정. `var/web-e2e-fixed-results.jsonl`(이전 실패: `var/web-e2e-diagnosis.jsonl`). `make test-web-e2e`로 재현한다.
6. **06번 확인 대기:** 실제 서비스 설치·로그온·부팅, 실제 72시간·전원 차단·15번 엣지 검증. T03-b는 실제 자식 강제 종료와 주입 시계 72시간/73회 단절 후 자동 재개·실영수증 검증까지 통과했다. DPAPI·Go SDK 및 서명 업데이트 작업 15·16은 로컬 검증 완료다.
7. 15번 엣지 수집기(T02-e·f) / 16번 NGSI-LD(T19) — 조건부, 하드웨어·요구 확정 후

**사람·현장 입력이 필요한 것 (NEEDS_INPUT·HARDWARE)**
8. T11-f 실제 관리기관 보고서 양식(HWPX) 확보 → 템플릿 등록
9. T13-a 외부 개발자 시험, T14 스테이징 Runbook 수행 기록(`docs/runbook/records/`)
10. T03-a 10GB 단절·재개, T12-b 부하 실측, T18 확장 실측 — 운영 하드웨어에서 (`loadtest/`)

**스펙별 남은 [ ] (자세히는 각 tasks.md)**
- 02: Keycloak MFA 단계 인증, 구성원 관리 (긴급 열람 완료, ADR-0029)
- 03: (작업 완료) 약정·연계 카드 API·좌표 변환 검증·기관 하위코드 등록·품질 재평가·웹 타입 생성
- 04: 예제 클라이언트·Schemathesis, 실제 악성코드 검사기 제품 결정 (웹훅·검사 포트/모의 완료, ADR-0030·0031)
- 05: Model_Registry 등록 API, GPU 인코더 실측, 대용량 임시 디스크 정책
- 06: 실제 PC 서비스 설치·부팅 확인, ROTATING은 15번과 함께. DPAPI·Go SDK·Windows 서명 업데이트 로컬 검증 완료
- 08: 백업 도구 PoC → ADR-0021 갱신, PITR·암호화·회차 정리, Hot/Warm, SSE (용량 예측·월간 리포트 완료, 전체 용량 값 입력 필요)
- 09: 온보딩 마법사, 회차 타임라인, 클립 내보내기, 운영 콘솔 편집, 접근성 CI
- 10: 실제 HWPX 양식, 정답 데이터로 '검증 필요' 해제
- 11: 운영 메신저 연동, 실제 하드웨어 신호 어댑터
- 12: 실제 기관 Open API 어댑터 (명세·이용조건 확인 후)
- 13: 대용량 데이터셋 버전 작업 큐 처리, 썸네일 샘플
- 14: 실제 탐지·임베딩 모델 선정(라이선스 검토) 후 교체 (ADR-0020), 날씨 태그

**사람이 결정할 것:** 5장 표 그대로 (운영 메신저, 모델, 백업 도구, 보고서 양식 등).

## 1. 지금 상태 한눈에
| 스펙 | 상태 | 남은 것 (자세히는 각 tasks.md) |
|---|---|---|
| 기준선 | 완료 | kiro-pack v0.2 + GPT v0.07 오픈소스 검토서(참고) |
| 01 개발 기반 | 거의 완료 | `make up` 실패 경로 자동 시험, golangci-lint(06번과 함께) |
| 02 인증·테넌트 | 핵심 완료 | 긴급 열람(요구사항 8) 완료. Keycloak MFA 단계 인증, 구성원 관리(요구사항 9) 남음 |
| 03 메타데이터 표준 | 완료 | 좌표 변환 검증·기관 하위 이벤트 코드 등록·품질 재평가·약정·연계 카드 API·계약 타입 생성(웹 TS·Go SDK, 백엔드는 스키마 검증) |
| 04 수집 API | 핵심 완료 | 웹훅·쿼터 동시성 시험·악성코드 검사 포트(모의) 완료. 검사기 제품 결정, 예제 클라이언트·Schemathesis, 10GB 시험 남음 |
| 06 동기화 에이전트 | 로컬 핵심 완료 | DPAPI·재개/영수증·계약 SDK·수동/자동 서명 업데이트 완료. 순환 삭제(15번과), 실제 서비스 설치·부팅 확인 남음 |
| 05 처리 파이프라인 | 핵심 완료 | 대용량 임시 디스크 정책, Model_Registry 등록 API(14번과), GPU 인코더 실측 |
| 07 센서·이벤트 | 핵심 완료 | 겹침 자산 조회 API·하위코드 등록 API 완료. MQTT 계정 보안 저장 남음 |
| 08 저장·보존 | 핵심 완료 | 보존·법적 보류·삭제 증명·쿼터 알림·사용량·일일 대조·계층형 백업·복원 시험(T10, ADR-0021 기준선) 완료. ROTATING 장비 백업 신청(0017)·용량 예측·월간 리포트(0018) 완료. 백업 도구 PoC(pgBackRest·rclone·restic)·PITR·암호화·Hot/Warm·SSE 남음, 전체 용량 값은 하드웨어 후 |
| 09 포털 | 핵심 화면 완료 | 기관 설정(연계 카드·웹훅·약정·긴급 열람)·백업 신청·운영 콘솔(약정·긴급 열람·백업)·계약 TS 타입 생성 추가. 온보딩 마법사(정책 초안·운영자 확인), 회차 타임라인, 클립 내보내기, 운영 콘솔 기관·사용자 편집, 접근성 CI 남음 |
| 11 수집 모니터링 | 핵심 완료 | 감시 규칙 13종·장애 기록·메일/웹훅 알림·정기 점검·하드웨어 모의 신호·T07 완료. Open API 실패 신호원(12번), 운영 메신저, 실제 하드웨어 어댑터 남음 |
| 10 증빙·보고서 | 핵심 완료 | 회차 기록 버전·증빙 승인·지표 4종·양식·보고서 초안/승인/제출·PDF·XLSX·HWPX·근거 손상·역추적·T11 완료. 실제 관리기관 양식(T11-f), 정답 데이터로 검증 필요 해제(14번) 남음 |
| 13 공유·데이터셋 | 핵심 완료 | 공유 규칙(속성 기반 T08)·우회 가드·후보·심사·등급 변경·데이터셋 버전·카탈로그·이용 신청·Grant·배포·철회·이용 이력·DCAT·T15 완료. 비식별 작업 처리(14번), 약정 API(03번), 썸네일 샘플 남음 |
| 14 비식별·검수 | 핵심 완료 (기준선) | 대상 제한·가림 렌더링·처리 이력·검수 배정/표본/결정·미탐 회차 재검수·품질 집계·태그·이벤트 규칙·장면 검색·T09 완료. **탐지·임베딩 모델은 모의/기준선** — 모델 선정 후 교체 (ADR-0020) |
| 12 Open API·환경 결합 | 핵심 완료 (모의 어댑터) | 소스 등록·주기 수집·보충·호출 한도·형식 변경 격리·경보·관측소 장비·환경 결합(거리·시각 차이)·재배포 제한·출처 표기·보고서 변수·환경 필터·T17 완료. **실제 기관 API 어댑터는 명세 확인 후** |
| 17 수용·릴리스 | 핵심 완료 | 세부 ID별 수용 보고(`make test-acceptance`)·가상 3사 여정 E2E(T06-d)·동시 업로드 쿼터(T12-a)·워커 정지(T12-c)·Locust 부하 시나리오·SBOM/Trivy/ZAP CI·Runbook 10종·릴리스 게이트·릴리스 노트. Go·실제 웹 E2E·로컬 보안 보고 연결 완료. 스테이징 Runbook 기록(T14), 운영 실측(T12-b·T18), 변경 후 원격 CI 확인 남음 |
| 15·16 | 시작 전 | 조건부 (엣지 하드웨어·연계 카드, NGSI-LD 요구 확정 후) |

**시험 (로컬 재검증):** 백엔드 고유 시험 192개는 2026-11-04 기준선. 2026-11-05 에이전트 Linux Go 전체 50개·11개 시험 패키지 기준선과 최종 업데이트 Linux 15개 race, Windows native 업데이트 14개·실제 CLI 통과. 세부 로그는 `var/go-06-final.json`, `var/update-no-console-windows-native.log`, `var/update-no-console-linux-race.log`, `var/*-baseline.*`, 수용 원본은 `var/acceptance-results.jsonl`.
수집 시험은 moto S3 서버와 실제 SeaweedFS(`TEST_S3_ENDPOINT`) 양쪽에서 통과.
**로컬 수용시험 누적(2026-11-05):** PASS 72 · NOT_IMPLEMENTED 6 · NEEDS_INPUT 4 · NEEDS_HARDWARE 2. 세부 ID별 표는 `docs/ACCEPTANCE-STATUS.md`. Windows 업데이트 시험은 수용 ID 밖의 별도 증거다.
릴리스 게이트에는 변경 후 원격 CI와 strict 라이선스 승인이 남아 있다. T01-h는 로컬 보안 보고서 연결이다. 과거 클라우드 M1 기록은 이번 릴리스 판정에서 제외한다.
**클라우드의 과거 개발 환경 확인 기록:** `make up` 7개 서비스 healthy. 실제 Keycloak 로그인 → 업로드 생성 → localhost:8333 사전 서명 URL로 파트 전송 → 워커 무결성 검증 → READY_PRIVATE → 영수증까지 확인. 이번 로컬 검증은 독립 PostgreSQL·모의 S3·MQTT와 공용 API 이미지로 수행했다.

## 2. 로컬에서 시작하기
필요한 것: Docker(Compose v2), Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 22, (06번부터) Go

```bash
git clone https://github.com/jungsoo7496-blip/OceanData.git
cd OceanData
cp .env.example .env          # 값은 개발용 자리표시자. 필요하면 바꾼다
make venv                     # backend/.venv에 개발 의존성 설치
make up                       # 전체 개발 환경 기동 (처음엔 이미지 빌드로 몇 분 걸림)
make seed                     # 시험 기관 5·사용자 9·사업 2·장비 5·회차 2
make test-integration         # 통합 시험 (make up의 PostgreSQL 사용)
make test-acceptance          # 수용시험 → var/acceptance.{json,md} (세부 ID별)
make release-gate MILESTONE=M1   # 계약·추적성·비밀값·라이선스·시험·마일스톤 케이스
make backup && make restore-test # 계층형 백업·복원 시험 (pg_dump 16 필요)
```

- API 문서: http://localhost:8000/docs · 웹 포털: http://localhost:5173 (member-a/dev-password로 로그인) · 포털 E2E: `cd web && node e2e/portal.mjs` · Keycloak: http://localhost:8080 (admin / .env의 KEYCLOAK_ADMIN_PASSWORD)
- 시험 계정: `admin-a`, `member-a`, `admin-b`, `member-b`, `viewer`, `operator`, `operator2`, `reviewer`, `consumer` / 비밀번호 `dev-password` (admin·operator는 첫 로그인 때 OTP 등록)
- 개발 토큰: `deploy/compose/keycloak/README.md`
- 회사·기관망에 SSL 검사 프록시가 있어 이미지 빌드 중 인증서 오류가 나면: 프록시 CA를 `deploy/certs/proxy-ca.crt`로 넣는다 (커밋 안 됨).

## 3. Claude Code로 이어서 하기
로컬 저장소에서 `claude`를 실행하고 이렇게 시작하면 된다.

```
HANDOFF.md, AGENTS.md, .kiro/steering/*.md를 읽고 각 스펙 tasks.md의 남은 [ ] 항목을 이어서 진행해줘.
먼저 docs/ACCEPTANCE-STATUS.md의 NOT_IMPLEMENTED 항목부터. 계약(contracts/)부터 고친 다음 구현해.
```

### GPT(Codex·ChatGPT)로 이어서 할 때
- Codex는 저장소 루트의 `AGENTS.md`를 자동으로 읽는다. ChatGPT(웹)라면 저장소를 연결하거나 아래 파일을 직접 올린다.
- 첫 지시 예시:

```
저장소 https://github.com/jungsoo7496-blip/OceanData 를 이어서 개발해줘.
먼저 AGENTS.md, HANDOFF.md(특히 0장 '남은 일'), .kiro/steering/*.md, docs/ROADMAP.md를 읽어.
규칙: 한 번에 스펙 하나, 계약(contracts/) 먼저 고치고 make contracts-check 통과 후 구현,
domain.md 코드값은 바꾸지 말고 제안만, 확인 안 된 외부 사양은 지어내지 말고 '확인 필요'+모의 구현,
완료 = 코드+시험+마이그레이션(down 포함)+계약·문서 갱신, 시험이 실패하면 성공이라고 하지 마.
0장 1번(전체 시험 재실행: make test-integration, make test-acceptance, make agent-test)부터 하고 결과를 보고해.
```

Kiro를 쓸 경우: Specs 패널에서 03번을 열고 README의 "Kiro에 줄 지시 예시"를 따른다. 01·02번 design.md·tasks.md는 이미 있다.

## 4. 구조 요약
```
backend/app/
  api/        system.py(헬스·기능), auth.py(토큰·등록·API 키·감사·me), context.py(사업·회차·장비),
              catalog.py(스키마·코드·사전 검증·정책·계보), uploads.py, assets.py, deps.py(인증·권한·범위)
  domain/     ids, principal, permissions(Permission_Table), states(상태 전이 표), metadata(기본값·variant 규칙),
              normalize(촬영시각·위치·단위), quality, errors   ← 프레임워크 import 금지(시험으로 검사)
  services/   auth, tokens, tenant(Tenant_Guard), audit(해시 체인), idempotency, contracts(검증기·번들),
              ingest(업로드·무결성 검증·격리·쿼터·영수증)
  ports/      8개 포트 / adapters/ S3·PgQueue·Keycloak·Notifier·Clock
  db/         models.py, migrations/versions/0001_*.py
  workers/    main.py (HANDLERS에 작업 유형 등록)
backend/tests/ unit · integration · contract(openapi 응답 검증) · acceptance(T01)
deploy/compose/ docker-compose.yml, postgres/(pgvector+PostGIS 이미지), keycloak/realm-marine.json
tools/         validate_contracts.py, check_traceability.py, check_licenses.py, check_secrets.py, compose_up.sh, sample_video.sh
```

새 엔드포인트를 만들 때 지킬 것:
1. `contracts/openapi.yaml`부터 고치고 `make contracts-check`
2. `PERMISSIONS`에 동작 추가 → 라우터에서 `Depends(require("동작"))`
3. 기관 소유 테이블 조회는 `scoped_select` / `load_scoped`만 사용 (직접 `select(Model)` 금지)
4. `backend/tests/acceptance/test_t01_tenant_isolation.py`의 `CROSS_ORG_CASES`에 교차 기관 케이스 추가
5. `backend/tests/contract/`에 응답 계약 검사 추가

## 5. 사람이 결정해야 할 것
| 항목 | 내용 | 위치 |
|---|---|---|
| C0 세션 취소 시 역전이 | 바이너리 없는 C0 자산에 연 업로드를 취소·만료하면 UPLOADING → READY_PRIVATE로 되돌린다. domain.md 5장 표에 없는 전이 | 04번 tasks "확인 필요" |
| LineageEdge.relation | SAMPLE_FRAME 값이 없어 THUMBNAIL로 기록 중 | 04번 tasks "확인 필요" |
| PSF-2.0 라이선스 예외 | typing_extensions·greenlet(FastAPI·SQLAlchemy 필수 의존성). 승인 전까지 검사는 경고만 | `docs/adr/0011-psf-license-exception.md` |
| Keycloak MFA 방식 | OTP 로그인 시 `acr`/`amr` 클레임이 나오게 단계 인증을 설정할지 | `deploy/compose/keycloak/README.md` |
| 구성원 초대 방식 | 플랫폼이 Keycloak 관리 API로 사용자를 만들지, IdP에서 만들고 연결만 할지 | 02번 tasks 12 |
| 공공 Open API 소스 | 어느 기관의 어떤 API(기상·해양 관측)를 쓸지, 인증·호출 한도·재배포 조건 확인. 확인 전에는 모의 어댑터만 | 12번 design.md |
| 비식별·태깅·임베딩 모델 | 라이선스·학습 데이터 조건을 보고 모델을 골라 Model_Registry에 등록. 지금은 합성 시험용 모의 탐지기라 실제 영상에서 아무것도 가리지 않는다 | `docs/adr/0020-deid-baseline-detector-embedder.md` |
| 보고서 PDF 라이선스 예외 | WeasyPrint가 끌고 오는 Pillow(MIT-CMU)·Pyphen(MPL-1.1 선택) 승인. 거절하면 `REPORT_FORMATS`에서 PDF를 빼고 의존성 제거 | `docs/adr/0018-report-rendering.md` |
| 실증지표·보고서 양식 | 시드의 지표·양식은 합성 예시. 실제 지표 정의(분자·분모)와 관리기관 양식(HWPX)을 받아 운영자가 등록 | 10번 design.md |
| 감시 규칙 기본 임계값 | 하트비트 3배·최소 5분, 지연 3배, 회차 2시간 유예·48시간 창, 현장 디스크 10GiB 등은 초안. 시범 운영 후 조정 (`PATCH /v1/watch-rules`) | 11번 design.md |
| 운영 메신저·알림 주소 | 알림은 메일(SMTP)+일반 웹훅만. 메신저 종류, 사용자 메일 주소를 IdP에서 가져올지 | ADR-0017, 11번 tasks 8 |
| 기존 결정 대기 | 전자정부 프레임워크 요구 여부, 운영 스토리지, 배포 형태 등 | `.kiro/steering/tech.md` 5장 |

## 6. 이번에 내린 설계 결정 (ADR)
- ADR-0011 PSF-2.0 예외 (승인 대기)
- ADR-0012 PostgreSQL 드라이버 pg8000 (psycopg는 LGPL이라 허용 목록 밖)
- ADR-0013 기기·API 키 토큰은 자체 발급 JWT, 폐기는 매 요청 DB 확인으로 즉시 반영
- ADR-0014 작업 큐는 PostgreSQL SKIP LOCKED 자체 구현 (procrastinate는 psycopg LGPL)
- ADR-0015 관측값 매핑 프로필 형식
- ADR-0016 웹 포털 의존성 (Uppy ARIA 알려진 문제)
- ADR-0017 수집 경보 상태·장애 자동 해결·ACK 시 재알림 중지·알림 수신 주소(`users.contact_email`)
- ADR-0020 비식별·태깅·검색 기준선(모의 탐지기·밝기 주야·태그 어휘 임베딩), 모델은 포트 뒤 — 선정 후 교체
- ADR-0019 공유 조건은 순수 함수 하나 + ORM 상향 가드, 데이터셋 항목 표·버전 등급은 현재 항목 등급으로
- ADR-0021 계층형 백업·복원 시험 기준선(대상 술어·증분 복사·스냅샷 행 수·pg_dump), 도구는 PoC 후 교체
- ADR-0022 백업 이미지 PG16 도구·Compose 설정/소유자 연결·주버전 검사 (API·DB 구조 변경 없음)
- ADR-0023 공급망·릴리스 검사 강화·실제 브라우저 탭 종료/재개
- ADR-0024 CurrentUser DPAPI·서명 업데이트 감독 (Windows native·제품 CLI 재검증 완료)
- ADR-0025 계약 SDK 생성용 참조 투영·고정 생성기·재생성/실제 호출 검사
- ADR-0031 악성코드 검사: 스트리밍 포트 + EICAR 모의, 기관 정책 플래그(0016), 검사기 없으면 미룸
- ADR-0030 웹훅: ORM outbox 발행·저장하지 않는 서명 비밀·SSRF 차단·N회 실패 시 비활성
- ADR-0029 긴급 열람: 요청자 한정·자산/회차 대상·만료 시각으로 즉시 회수·접근마다 감사
- ADR-0028 약정·연계 카드 API: 약정 종료는 valid_until, 카드는 분야별 자유 기록 + 정책 코드 검사
- ADR-0018 보고서 내보내기: PDF(WeasyPrint)·XLSX(openpyxl)·HWPX(양식 XML 치환), Pillow·Pyphen 예외 승인 대기

## 7. 알아 둘 점
- 감사 로그(audit_log)는 트리거로 수정·삭제가 막혀 있다. 개발 DB를 비우려면 `make down` 후 볼륨 삭제(`docker compose ... down -v`).
- 통합 시험은 `TEST_DATABASE_URL`의 DB 서버에 임시 DB를 만들고 끝나면 지운다. `make up`의 postgres(5432)를 쓰면 된다.
- 공유 시험 흐름(개발): 약정 `python -m app.cli agreement <기관ID> --reference 합성 --purposes AI_TRAINING`, 비식별 작업 대신 `python -m app.cli share-processed <원본ID>`(14번 전까지).
- 알림 메일 확인: `.env`에 `SMTP_HOST=mailpit` 후 `docker compose --env-file .env -f deploy/compose/docker-compose.yml --profile ops up -d mailpit`, 받은 메일은 http://localhost:8025. 워커가 `WATCH_EVAL_INTERVAL_S`마다 감시 규칙을 평가한다. 수동 평가: `python -m app.cli watch`, 모의 하드웨어 신호: `python -m app.cli hardware-signal --status CRITICAL`.
- 운영 절차는 `docs/runbook/` (10종). 스테이징에서 수행하면 `docs/runbook/records/`에 기록을 남긴다 (T14).
- 부하 시험: `pip install -r loadtest/requirements.txt` 후 `make loadtest LOAD_HOST=... ` (loadtest/README.md). SBOM·취약점: syft·trivy 설치 후 `make sbom`, `make vuln-scan`.
- `.github/workflows/ci.yml`·`security.yml`은 GitHub에 push하면 돈다. 기존 main 실행의 실패와 후속 우선순위는 0장 2번 참고. 이번 08·17·06번 변경은 아직 원격 CI 미실행이다.
