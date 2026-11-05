# 2026-11-05 점검 기록 — 보안·정확성 검토와 계약 대비 구현 점검

클라우드 세션 마지막 날 세 가지 점검을 했다. 이 폴더는 **무엇을 찾았고, 무엇을 고쳤고, 무엇이 남았는지**와 재현 방법을 모은다.
진행 상황의 최신 요약은 루트 `HANDOFF.md`의 '남은 일'이다. 이 문서는 근거 자료다.

| 점검 | 방법 | 결과 파일 |
|---|---|---|
| 1. 그날 추가한 서버 코드 검토 | 읽기 전용 검토 + 재현, 확인된 것만 수정 | HANDOFF 0장 -1u (모두 처리) |
| 2. 예전 서버 코드 검토 | 읽기 전용 검토 + 재현 | 아래 '검토 2' |
| 3. 계약 문구 대비 구현 점검 | 작업별로 계약 설명과 구현 비교, 발견자가 재현 | [contract-sweep-findings.md](contract-sweep-findings.md) |

## 검토 2 — 예전 서버 코드 (재현됨)

상태: **고침**(`fix/backup-runtime-pg16`에 병합) / **미완**(`wip/*` 브랜치에 진행분, 검토 전) / **미착수**.
재현 파일은 `repro/older-code-review/`다 (아래 '재현 파일 쓰는 법').

| # | 심각도 | 내용 | 상태 |
|---|---|---|---|
| 1 | 높음 | `parent_ref` 업로드는 취소·만료가 안 되고(계보 FK로 자산 삭제 실패 → 500), 만료 정리가 한 트랜잭션이라 한 줄 실패가 모든 기관의 정리를 막는다 | **고침** — 계보 행을 먼저 지우고 만료 정리는 세션마다 SAVEPOINT |
| 2 | 높음 | 선언 크기를 강제하지 않아 1바이트로 선언하고 파트마다 최대 5 GiB를 올려 쿼터를 넘긴다. 격리 용량 계산도 더할 땐 실제 크기·뺄 땐 선언 크기 | **미완** — `wip/ingest-size-enforcement` |
| 3 | 중간 | 장비 자격(status:read·upload:write)이 보고서 초안·회차 기록 작성·매핑 밖 관측값 열람까지 된다 | **고침** — Permission_Table `device` 표시 + Tenant_Guard 기기 범위 (`test_device_reach.py`) |
| 4 | 중간 | DB 커밋이 응답을 보낸 뒤에 일어나 롤백된 작업에 2xx를 줄 수 있다. 같은 멱등 키 동시 요청 둘 다 201 | **미완** — `wip/commit-before-response-tests`(재현 시험만. 구현은 첫 시도를 되돌림) |
| 5 | 중간 | 바이트가 없는 파일(만료 미정리 세션·체크섬 불일치 격리)에도 `ALREADY_REGISTERED` → 에이전트가 다시 보내지 않는다(순환 녹화 원천은 조용한 유실) | **미착수** |
| 6 | 중간 | 업로드 중인 자산·회차를 삭제하면 거짓 삭제 증명, 완료 뒤 객체가 고아로 남는다 | **미착수** |
| 7 | 중간 | 인증 없는 요청마다 `AUTH_FAILED` 감사 행(원래 경로)·전역 감사 잠금, 토큰 한도 키가 `client_id`로 우회·무한 증가, 모르는 kid마다 잠금 안에서 JWKS 재조회 | **미착수** |
| 8 | 낮음~중간 | 완료와 취소(또는 만료 정리)가 겹치면 쿼터 밖 고아 객체 | **미착수** (`SELECT … FOR UPDATE`) |
| 9 | 낮음~중간 | XLSX 보고서에 `=`로 시작하는 사용자 문구가 수식으로 들어간다 | **고침** |
| 10 | 낮음 | 단위 경고 하나가 관측값 묶음의 스키마 오류를 가려 400 대신 500 | **고침** |
| 11 | 낮음 | 장비가 `human_verified=true` 이벤트를 만든다 | **고침** — 기기·API 키는 403 |
| 추정 | 낮음 | 장비 자격 폐기 뒤 쓰지 않은 등록 코드로 재등록 | **미완** — `wip/auth-enrollment-codes` |
| 추정 | 낮음 | 웹 프록시 뒤에서 등록 요청 한도가 모든 클라이언트 한 묶음(uvicorn `--proxy-headers`는 127.0.0.1만 신뢰) | **미착수**(배포 설정) |
| 추정 | 낮음 | 보고서 지표 출처에 PRIVATE 자산 ID, 다른 기관에 `original_filename`·`original_relative_path` 노출 | **미완** — `wip/report-scope-leaks` |

**사람이 정할 것:** 운영자가 모든 기관의 원 관측값을 긴급 열람 없이 읽는 것, 운영자의 다른 기관 보고서 초안 열람(감사 없음)이 맞는지.

## 재현 파일 쓰는 법

`repro/` 아래 파일은 **참고용이다 — 저장소의 시험이 아니다**(`backend/tests` 밖이라 pytest가 모으지 않는다). 재현하려면
`backend/tests/` 안으로 복사해(예: `tests/integration/`) 자기 DB로 돌린다. 업로드 재현은 모의 S3 `ictx` 고정물을, 나머지는 `ctx`를 쓴다.

```bash
cd backend
createdb ... marine_repro            # 또는 psql로 CREATE DATABASE marine_repro (운영 DB·marine은 쓰지 않는다)
cp ../docs/review-2026-11-05/repro/older-code-review/test_rv_uploads.py tests/integration/
TEST_DATABASE_URL=postgresql+pg8000://marine_owner:change-me-owner@127.0.0.1:5432/marine_repro \
  .venv/bin/pytest -q -p no:cacheprovider -p no:logging tests/integration/test_rv_uploads.py -k lineage
```

재현 시험은 **고쳐지기 전 동작(버그)을 보이려고** 쓴 것이라, 고친 뒤에는 기대가 바뀌어야 한다. 고칠 때는 이 파일을 정식 회귀 시험으로
다시 써서 `backend/tests/`에 넣고 이 폴더의 복사본은 지운다. `.wip` 파일은 중단된 구현에서 나온 시험이다.
