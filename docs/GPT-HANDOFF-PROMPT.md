# GPT(Codex·ChatGPT)에게 이어 시킬 작업 지시서

2026-11-05 클라우드 세션이 끝난 시점(`fix/backup-runtime-pg16`)의 남은 일을 GPT에게 넘길 때 쓴다. 두 부분이다.

- **붙여 넣을 글**(아래 '첫 메시지', 끊겼을 때의 '후속 프롬프트'): 짧다. GPT가 이 저장소를 받아 이 문서를 읽게 만든다.
- **작업 지시서**(그 아래 [0]~[6]): 규칙·기준선·해야 할 일·하지 말 것·멈춤 규칙. GPT가 읽고 그대로 따른다.

남은 일의 정본은 `HANDOFF.md`의 '남은 일' 절이다. 이 문서는 그 항목을 **주제별로 묶고 순서·크기를 붙인 것**이라 번호가 HANDOFF의 순서와 같지 않다.
항목을 끝내면 HANDOFF를 고친다. 정본이 바뀌면 이 문서도 같이 고친다.
Codex는 저장소 루트의 `AGENTS.md`를 자동으로 읽는다. ChatGPT(웹)는 셸·git·Docker를 실행하지 못하므로 이 지시서의 대부분을 할 수 없다 — [2]의 '실행 전제'를 따른다.

## 붙여 넣을 글 — 첫 메시지

붙여 넣기 전에 확인할 것 3가지: ① Codex를 이 저장소 폴더에서 연다(저장소가 없으면 빈 폴더에서 연다 — 그러면 GPT가 clone한다). ② Docker Desktop을 켜 둔다(꺼져 있으면 시험 대부분이 못 돈다). ③ 이 컴퓨터에서 GitHub에 push가 되는지(로그인) 확인한다 — 안 되면 GPT가 만든 작업이 컴퓨터에만 남고, 사용량이 끊기면 이어 하기 어렵다. ChatGPT 웹이면 코드 읽기와 변경안만 나온다. 아래 4)의 마지막 항목(AGENTS.md 2번 해석)은 소유자가 정하는 것이니 동의하지 않으면 그 줄을 지운다.

```
OceanData(해양 규제자유특구 실증데이터 플랫폼, https://github.com/jungsoo7496-blip/OceanData) 개발을 이어받는다.
앞선 개발자(Claude 클라우드 세션)가 사용량 한도로 일부를 끝내지 못했다. 이 대화에는 이전 지시가 없다.

1) 저장소를 받는다. 이미 저장소 안이면 clone 대신 git status를 보고(커밋 안 된 변경이 있으면 지우지 말고 알린다) git fetch origin 한 뒤
   git switch fix/backup-runtime-pg16 && git pull --ff-only 로 최신 fix 브랜치로 간다. main에는 지시서가 없고, main에는 합치지 않는다 — 합치는 건 사람이 한다.
   (GitHub 화면·connector로만 읽는 도구라면 https://github.com/jungsoo7496-blip/OceanData/blob/fix/backup-runtime-pg16/docs/GPT-HANDOFF-PROMPT.md 를 연다.)
2) 다음을 끝까지 읽는다: AGENTS.md → HANDOFF.md('요약'·'남은 일'·5장·7장) → docs/GPT-HANDOFF-PROMPT.md 전체.
   마지막 문서가 작업 지시서다. 규칙·기준선·해야 할 일(1~13번)·하지 말 것·작업 방식·멈춤 규칙이 다 있고, 그대로 따른다.
3) 사용량이 허용하는 만큼, 지시서의 권장 실행 순서대로 한 번에 한 항목씩 진행한다.
   항목마다 gpt/<NN>-<이름> 브랜치(NN = 항목번호 두 자리, 예 gpt/06-auth-enrollment-codes)를 만들어 거기서 일한다:
   재현 시험 먼저 → API가 바뀌면 계약(contracts/) 먼저 → 최소 구현 → 시험 → 수시로 'WIP(미완) <항목번호> …' 커밋·push.
   fix/backup-runtime-pg16에는 끝난 항목만 지시서 [5]의 방법(squash, 제목 '[지시서 N번] …')으로 합치고, HANDOFF.md 갱신은 그 커밋에 함께 넣는다.
4) 어기면 안 되는 것(자세한 건 지시서):
   - 시험이 실패하면 성공이라고 하지 말고 실패 출력을 그대로 보고한다.
   - .kiro/steering/domain.md 의 코드값·상태 전이·규칙은 바꾸지 말고 제안만 쓰고 그 부분에서 멈춘다.
   - 확인되지 않은 외부 사실은 지어내지 말고 '확인 필요' + 모의 구현.
   - git --force 금지, main 병합·PR 생성 금지. 실제 영상·개인정보·비밀값 금지(합성 데이터만).
   - AGENTS.md 2번(스펙 하나씩)은 5·11·12번처럼 여러 스펙에 걸친 버그·보안 수정에서는 "다른 스펙의 새 기능을 미리 만들지 말라"는 뜻으로만 적용한다(소유자가 허용).
5) 셸·git·Docker를 실행할 수 없는 환경(일반 채팅)이면 시험을 돌린 것처럼 쓰지 말고, 실행하지 못했다고 먼저 밝힌 뒤 코드 읽기와 변경안만 낸다.
6) 먼저 지시서 [2]의 기준선을 돌려 결과를 숫자와 함께 보고하고, 어떤 항목부터 할지 한 줄로 말한 뒤 시작해라.
```

## 붙여 넣을 글 — 후속 프롬프트 (끊겼거나 다음 항목을 시킬 때)

`<항목번호>`는 아래 [3]의 1~13 중 하나다. 비워 두면 GPT가 권장 실행 순서에서 아직 안 합쳐진 첫 항목을 고른다.

```
OceanData(https://github.com/jungsoo7496-blip/OceanData) 개발을 이어받는다. 이전 GPT 세션이 사용량 한도로 끊겼을 수 있고, 이 대화에는 이전 지시가 없다.
1) git status 와 git branch --show-current 부터 본다.
   - rebase·merge가 충돌로 멈춰 있으면 WIP 커밋을 만들지 말고(충돌 표시가 커밋된다) 상태를 사용자에게 알린 뒤 지시서 [3]의 규칙대로 푼다(풀 수 없으면 git rebase --abort 후 보고).
   - 커밋 안 된 변경이 있으면 지우지 말고, 현재 브랜치가 fix/backup-runtime-pg16이면 먼저 gpt/<NN>-<이름> 브랜치를 만든 뒤 거기에 'WIP(미완) <항목번호> …' 커밋으로 남긴다(WIP는 fix에 커밋하지 않는다).
2) git fetch origin 뒤 fix 최신을 확인한다(git log origin/fix/backup-runtime-pg16 --oneline -15). 이번 항목(NN = 항목번호 두 자리)의 이전 작업 브랜치는
   git branch -r --list 'origin/gpt/<NN>-*' 로 찾는다. 있으면 그 브랜치로 이어 가고(git log -3 으로 마지막 WIP 메시지의 '다음에 할 것'을 읽는다), 없으면 지시서 [3]대로 새로 시작한다.
   번호가 다른 gpt/* 브랜치는 이미 합쳐졌거나 다른 항목이니 건드리지 않는다.
3) AGENTS.md, HANDOFF.md '남은 일', docs/GPT-HANDOFF-PROMPT.md 전체를 읽고 그대로 따른다. 특히 [1] 규칙, [4] 하지 말 것, [5] 작업 방식, [6] 멈춤 규칙:
   --force 금지, main 병합·PR 금지, domain.md 수정 금지, 지어내기 금지, 실패는 그대로 보고.
4) 이번에 할 일: 지시서 [3]의 <항목번호>번 하나. (비워 뒀으면 권장 실행 순서에서 fix에 아직 안 합쳐진 첫 항목 — 합친 항목은
   git log origin/fix/backup-runtime-pg16 --oneline --grep='^\[지시서' 로 확인한다.) 끝나면 보고하고 멈춘다.
```

---

# 작업 지시서 (GPT가 읽는 본문)

## [0] 먼저 읽을 것 — 이 순서로, 큰 파일은 grep으로 필요한 곳만

1. `AGENTS.md` — 지켜야 할 규칙 11개. 정본이다.
2. `HANDOFF.md` — '요약', '남은 일', 5장(사람이 결정할 것), 7장(알아 둘 점).
3. `.kiro/steering/*.md` — `domain.md`(코드값·상태 규칙의 정본), `security.md`, `api-conventions.md`, `tech.md`.
4. `docs/ROADMAP.md`, `docs/review-2026-11-05/README.md`(예전 코드 검토 결과 — 무엇이 고쳐졌고 무엇이 남았는지).
5. 항목마다 해당 스펙 폴더(폴더 이름 전체, 예 `.kiro/specs/04-ingestion-api/`)의 `requirements.md`·`design.md`·`tasks.md`.
   스펙: 1·2·3·4번 = 04-ingestion-api(3번은 08-storage-lifecycle도), 6·7번 = 02-identity-tenancy, 8번 = 10-evidence-reports, 9번 = 06-sync-agent,
   10번 = 08-storage-lifecycle, 13번 = 11-collection-monitoring, 5·11·12번 = 여러 스펙에 걸친 버그·보안 수정(AGENTS.md 2번 '스펙 하나씩'은 새 기능을 미리 만들지 말라는 뜻으로만 적용).

## [1] 규칙 — AGENTS.md 요약. 하나라도 어기면 그 항목은 완료가 아니다

- 시작 전에 steering·ROADMAP·해당 스펙의 requirements.md를 읽는다. 한 번에 스펙 하나만 진행하고 다른 스펙의 기능을 미리 만들지 않는다.
- 계약 먼저: API·스키마를 바꾸면 `contracts/` 수정 → `make contracts-check` 통과 → 구현 → 계약 시험.
- `.kiro/steering/domain.md`의 코드값·상태 전이·규칙은 사람의 승인 없이 바꾸지 않는다. 바꿔야 한다고 판단되면 제안만 쓰고 그 부분에서 멈춘다.
- 확인되지 않은 외부 사실(장비 사양, 외부 API 명세, 보고서 양식)은 지어내지 않는다. '확인 필요'로 적고 어댑터 인터페이스와 모의 구현만 만든다.
- 직접 만들기 전에 `docs/OSS-CANDIDATES.md`를 본다. '안 씀' 항목은 쓰지 않고, 목록에 없는 의존성은 라이선스 허용 목록(`.kiro/steering/tech.md` 3장)을 확인한 뒤 ADR을 남긴다.
- 완료 = 코드 + 시험 + 마이그레이션(down 포함) + 계약·문서 갱신. 하나라도 빠지면 완료로 표시하지 않는다. DB·계약을 안 바꾸는 항목은 보고에 '새 마이그레이션 없음 / 계약 변경 없음'이라고 적는다.
- 각 스펙 `tasks.md`의 작업 줄에는 그 작업이 실제로 구현하는 요구사항 번호를 단다(번호 순서대로 짝짓지 않는다).
- 시험이 실패하면 성공이라고 보고하지 않는다. 실패 내용과 재현 방법을 그대로 보고한다.
- 새로 정한 설계 결정은 `docs/adr/`에 남긴다(`0000-template.md` 양식). 필수다.
- 시험 데이터는 합성만. 실제 영상·개인정보·비밀값을 저장소에 넣지 않는다.

## [2] 환경과 기준선

**실행 전제.** 이 지시서는 셸·git·Docker를 실행할 수 있는 에이전트(Codex 등)를 위한 것이다. 명령을 실행할 수 없으면 시험을 돌린 것처럼 쓰지 말고, 실행하지 못했다고 먼저 밝힌 뒤 코드 읽기와 변경안 제안만 한다. 실행하지 않은 명령의 결과는 쓰지 않는다.
이미 저장소 안이면 clone은 건너뛰고 `git status`부터 본다. git `user.name`·`user.email`이 없으면 이 저장소에서만(`--local`) 설정한다.

**준비** (`HANDOFF.md` 2장이 정본):

    git clone https://github.com/jungsoo7496-blip/OceanData.git && cd OceanData
    git fetch origin && git switch fix/backup-runtime-pg16 && git pull --ff-only
    cp -n .env.example .env       # 이미 .env가 있으면 덮어쓰지 않는다
    make venv && (cd web && npm ci)
    make up            # Docker: PostgreSQL·SeaweedFS·Keycloak 등 (처음엔 몇 분)

필요: Docker, Python 3.12+, uv, Node 22, Go, 호스트에서 백엔드 시험을 돌리면 ffmpeg·ffprobe와 PostgreSQL 16 클라이언트(`pg_dump`·`pg_restore`).
없으면 해당 시험이 '실패'가 아니라 '건너뜀'으로 끝나 통과 수만 줄어든다. PATH의 `pg_dump` 주버전이 16이 아니면 `PG_DUMP_BIN`·`PG_RESTORE_BIN`으로 16 도구를 지정한다(안 하면 T10이 실패한다).
Windows이고 `make`가 없으면 Makefile의 대상을 읽고 같은 명령을 옮겨 실행한다: `.venv/bin/X` → `backend/.venv/Scripts/X.exe`, `VAR=값 명령` → PowerShell `$env:VAR='값'; 명령`.

**기준선** — 마지막 확인: 백엔드 `381 passed, 5 skipped`(skipped 5개는 `SCHEMATHESIS_ALL=1`로만 도는 선택 탐색). 모두 저장소 루트에서, 시스템 `python`이 아니라 `make` 대상이나 `backend/.venv`의 python으로 돌린다
(시스템 python으로 `tools/validate_contracts.py`를 돌리면 openapi-spec-validator가 없어 '구조 검사만 했음'이라 하면서도 '통과'로 끝난다). **처음에는 1)·2)만 돌려 숫자를 기록한다.** 3)~5)는 그 항목이 해당 영역을 건드릴 때 돌린다.

    export TEST_DATABASE_URL=postgresql+pg8000://marine_owner:change-me-owner@127.0.0.1:5432/marine   # 개발용 자리표시자 값 (make up의 DB)
    1) make ci                                    # 계약·추적성·lint·타입·단위·보안·라이선스·비밀값 (스택은 필요 없지만 네트워크는 필요)
    2) mkdir -p var && (cd backend && .venv/bin/pytest -q -p no:cacheprovider -p no:logging) > var/pytest.log 2>&1; tail -5 var/pytest.log
    3) make agent-test agent-lint agent-generate-check
    4) (cd web && npm run lint && npm run typecheck && npm run check:api) && make web-a11y      # web-a11y는 빌드 포함. 처음 한 번 Chromium 필요: web/e2e/README.md
    5) backend/.venv/bin/python tools/migration_check.py                                          # head→base→head→base

긴 시험(수 분~10분)은 위처럼 로그 파일로 돌려 명령 제한 시간에 걸려도 결과를 잃지 않게 한다.
계약을 바꿨다면 반드시: `make contracts-check`, `(cd web && npm run gen:api && npm run check:api)`, `make agent-generate agent-generate-check`.
**원격 CI:** `ci`·`web-e2e`는 `main`에 push할 때와 `pull_request`에서만, `security`는 main push·주간·수동에서만 돈다. `fix/backup-runtime-pg16`이나 `gpt/*`에 push해도 CI는 시작되지 않는다.
CI 결과가 필요하면 사용자에게 "fix/backup-runtime-pg16 → main draft PR을 열어 달라"고 요청한다(네가 PR을 만들지는 않는다). 그 전에는 보고에 '원격 CI 미실행, 로컬 기준선만'이라고 적는다.
기준선이 코드 때문에 실패하면 멈추고 보고한다. 환경 한계(네트워크·Docker·도구 없음)는 코드 실패가 아니다 — [6] 참고.
환경 준비(`make venv`·`npm ci`·`make up`)나 기준선에서 같은 오류로 두 번 막히면 더 고치려 하지 말고, 막힌 명령과 오류 전문을 보고한 뒤 그 도구가 필요 없는 일(코드 읽기·계약·문서·Go 쪽)부터 한다. 환경을 고치느라 사용량을 쓰지 않는다. `git push`가 인증·네트워크로 실패해도 두 번 재시도하지 말고 로컬 커밋만 남기고 사용자에게 알린다.

## [3] 해야 할 일

**권장 실행 순서** (사용량이 모자랄 수 있으니 끝내기 쉬운 것·가장 중요한 것을 먼저 합치고, 모든 라우터를 건드리는 5번은 마지막에 한다. S 반나절 미만 · M 하루 · L 이틀 이상):

    3(M) → 8(M) → 7(M) → 13(S) → 10(L) → 11(L) → 12(L) → 5(L)

6번·9번·1번·4번과 2번의 만료 세션 보완은 완료했다(`HANDOFF.md` 0장 -1ac부터 -1ag). 2번의 체크섬 격리 재수집은 5장 사람 결정 항목이다.

12번은 HANDOFF에서 '먼저 확인할 것'이다. 웹훅 격리·기기 범위처럼 보안에 닿는 병합분은 사용량이 남으면 앞당겨도 된다.

**미완 브랜치 `wip/*` 이어 하는 법** (원격에 6개, 모두 미완·검토 전, 예전 커밋 기반). 항목마다 `gpt/<NN>-<이름>`(NN = 항목번호 두 자리, 예 `gpt/06-auth-enrollment-codes`)을 만들어 거기서 일한다:

    git fetch origin
    git switch -c gpt/<NN>-<이름> --no-track origin/wip/<이름>   # --no-track: 업스트림이 wip 브랜치가 되지 않게 한다 (git이 권하는 'git push origin HEAD:wip/…'는 하지 않는다)
    git rebase origin/fix/backup-runtime-pg16
    # 계약 검사·재생성을 바로 돌린다 (충돌이 없어도 생성 파일이 낡을 수 있다: wip/auth-enrollment-codes·wip/report-scope-leaks는 rebase 뒤 npm run check:api가 실패한다)
    make contracts-check; (cd web && npm run gen:api && npm run check:api); make agent-generate agent-generate-check
    git push -u origin HEAD                                  # gpt/<NN>-<이름> 으로 첫 push

rebase 중 'skipped previously applied commit' 경고는 이미 fix에 병합된 옛 커밋을 git이 건너뛴 것이라 정상이다. 예상 밖 충돌이 나면 `contracts/openapi.yaml`은 손으로 풀어 양쪽 변경을 모두 살리고,
생성 파일(`web/src/contract/api.gen.ts`, `agent/internal/contractapi/client.gen.go`)은 충돌 부분을 버리고 재생성한다. 풀 수 없으면 `git rebase --abort`하고 보고한다.
wip가 없는 항목은 `git switch -c gpt/<NN>-<주제> --no-track origin/fix/backup-runtime-pg16`에서 시작한다. **wip 브랜치는 지우지 않는다**(HANDOFF 표에 '완료' 표시만).

### A. 업로드·보관 정합성 (같은 파일을 만지므로 4 → 3 순서로, 1·2는 먼저)

**1) 업로드 선언 크기 강제 (완료, 2026-11-06).** Content-Length 서명과 완료 전 크기·ETag 확인, 불일치 정리 및 예약 보호를 반영했다. 실제 SeaweedFS 3.80 서명·브라우저 재개와 백엔드 전체 시험을 확인했다(ADR-0044, `HANDOFF.md` 0장 -1ae).
재현 원본 `docs/review-2026-11-05/repro/older-code-review/test_rv_uploads.py`의 `test_rv_oversized_part_bypasses_quota`는 단언이 없는 참고용이다. WIP의 `test_complete_rejects_parts_larger_than_declared`가 정식판이니 끝나면 repro 사본은 지운다.

**2) 만료 세션의 `ALREADY_REGISTERED` 오판 (만료 세션 보완 완료, 2026-11-06).** 기존 만료 정리 뒤 새 세션을 열고 C0 자산 ID를 유지한다(ADR-0045). 체크섬 불일치 격리 재수집과 기존 영수증 대기 에이전트 복구는 `HANDOFF.md` 5장 확인 대기다.

**4) 완료·취소·만료 경합 독립 검증 (완료, 2026-11-06).** 1번의 세션 잠금을 동시 DELETE·요청 중 만료·주기 정리에서 확인했다. 잠금 전 500·고아 객체를 재현했고 현재 시험 3건이 통과했다(ADR-0044, `HANDOFF.md` 0장 -1ag).

**3) 검토 2 #6 — 업로드 중 삭제는 거짓 삭제 증명 (M)** — 업로드 중인 자산·회차를 삭제하면 삭제 증명이 나오고, 완료 뒤 객체가 고아로 남는다(`backend/app/services/retention.py` `delete_assets`, `backend/app/api/lifecycle.py` 삭제 경로).
UPLOADING 자산은 업로드를 중단(S3 abort + 쿼터 예약 해제)한 뒤 삭제한다. RECEIVED·VALIDATING은 이미 객체와 무결성 검증 작업이 있어 S3 abort가 불가능하므로, 삭제를 409(잠시 뒤 다시)로 거부하거나 검증기가 삭제된 자산을 건드리지 않게 만든다
(쿼터를 두 번 빼거나 지워진 자산을 REGISTERED로 되돌리지 않게). 어느 쪽이든 새 설계 결정이므로 ADR은 필수다 — 08번 요구사항의 삭제 증명과 어긋나지 않는 근거를 적는다. 재현: `repro/older-code-review/test_rv_uploads.py::test_rv_delete_during_upload`.

### B. 인증·감사

**6) 장비 자격 폐기 시 미사용 등록 코드 무효화 (완료, 2026-11-06).** 발급·등록·폐기를 수집기 행 잠금으로 직렬화하고 폐기 때 미사용 유효 코드를 만료한다. 운영 정책 확인은 `HANDOFF.md` 5장에 남겼다(ADR-0043).

**7) 검토 2 #7 — 인증 실패 남용 (M)** — 인증 실패 요청마다 `AUTH_FAILED` 감사 행(원래 경로 그대로)과 전역 감사 잠금, 토큰 발급 한도 키가 `client_id`로 우회되고 무한 증가, 모르는 `kid`마다 잠금 안에서 JWKS 재조회(`backend/app/api/deps.py` `get_principal`, 한도 `backend/app/ratelimit.py`(Limiter)·`backend/app/api/auth.py`(`_auth_limiter`), `backend/app/adapters/keycloak_identity.py`).
`security.md`는 로그인 실패를 '반드시 기록'하는 감사 항목으로 정하고 감사는 해시 체인이므로 **감사 기록 수는 줄이지 않는다.** 한 행에 담는 값만 제한한다 — 요청 경로는 라우트 템플릿(없으면 앞 100자)으로. 같은 (IP, 사유)를 분당 한 행 + 건수로 합치는 집계는 행 수를 줄이므로
구현하지 말고 선택지·장단점·추천을 ADR 초안과 `HANDOFF.md` 5장에 올린다. 토큰 발급 한도 키는 LRU 상한을 둔다(지금 키는 `{IP}:{client_id}`라 `client_id`만 바꾸면 새 버킷이 생기고 무한히 늘어난다 — `client_id` 없는 IP 단독 키도 함께 검사한다). JWKS는 모르는 `kid` 재조회에 최소 간격을 두고 잠금 밖에서 가져오되,
키 회전 때 정상 토큰이 막히지 않게(처음 보는 `kid`는 한 번 허용) 한다. 재현 파일(`repro/older-code-review/test_rv_misc.py`의 `test_rv_unauthenticated_audit_amplification`, `probe_jwks.py`)은 단언이 없는 참고용이고
`probe_jwks.py`는 3행 `sys.path`의 절대 경로를 자기 `backend/` 경로로 고쳐야 한다 — 수정 전에 실패하는 회귀 시험을 따로 쓴다.

### C. 노출·에이전트·성능

**8) `wip/report-scope-leaks` 마무리 (M)** — 보고서 지표 출처(`sources.asset_ids`)와 trace가 호출자에게 안 보이는 자산 ID(PRIVATE)를 내보내고(`backend/app/services/reports.py`, `backend/app/api/reports.py`), `asset_view`(`backend/app/api/assets.py`)가
소유 기관이 아닌 사용자에게도 `provenance.original_filename`·`original_relative_path`를 그대로 준다. 근거는 `security.md` 24행 '원래 파일명은 객체 키에 넣지 않는다 (파일명에 개인정보가 섞일 수 있다)'와 `04-ingestion-api/design.md`(원래 파일명은 provenance에만)이다 —
06번 요구사항 4.3은 에이전트가 이 값을 보낸다는 것까지만 정하고 노출 범위는 정하지 않는다. 우선 소유 기관 사용자와 PLATFORM_OPERATOR(domain.md 2장: 모든 기관 메타데이터)에게만 보이게 하고 관리기관 열람자·검수자·이용기관에게는 숨긴다.
이 노출 범위는 새 설계 결정이므로 ADR에 근거를 적고, 운영자에게도 숨길지는 구현하지 말고 `HANDOFF.md` 5장에 질문으로 올린다. 보이는 것만 내보내는 부분은 계약·시험 포함(rebase 뒤 웹 타입 재생성 필수).

**9) 에이전트 정책 숫자 넘침 방어 (완료, 2026-11-06).** 계약·서버·에이전트 상한, 구형 저장본 배포 검사, 유효한 이전 정책 유지, Retry-After·토큰·mvhd 회귀시험을 반영했다(ADR-0040, `HANDOFF.md` 0장 -1ad). 핫패스 ADR은 10번에서 0042로 옮긴다.

**13) 작은 것 (S)** — 운영자 알림 웹훅 `monitoring.post_webhook`(`ALERT_WEBHOOK_URL`)이 아직 옛 방식(단계별 시간 제한·본문 전체 읽기·잡 루프에서 실행)이라 느린 수신 서버가 잡 루프를 멈출 수 있다. ADR-0041(웹훅 전송 격리)과 같은 방식으로
총 기한·본문 안 읽기·전송 스레드 분리를 적용한다. 프록시 환경 변수 처리와 SSRF 정책은 바꾸지 않는다(프록시 전용 망의 웹훅은 사람 결정 — `HANDOFF.md` 5장).

**10) `wip/hot-path-indexes` 재측정·마무리 (L)** — 마이그레이션 0024(down 포함), 시험, ADR 초안. 브랜치의 ADR에는 전·후 측정 표가 이미 있고 상태도 'accepted'지만 측정에 쓴 DB와 생성 스크립트가 남아 있지 않아 **재현·검토되지 않은 값이다 — 믿지 않는다.**
ADR '맥락'의 규모(자산 50만·작업 109만·업로드 27만·관측값 177만·감사 150만 등)를 `generate_series`로 **새 임시 DB**(예 `marine_perf_<난수>`)에 합성해 같은 방법(`EXPLAIN (ANALYZE, BUFFERS)`, 7회 중앙값)으로 다시 재고 표를 네가 잰 값으로 바꾼다.
못 잰 줄은 '재측정 못 함'으로 남기고 효과가 확인되지 않은 인덱스는 뺀다. 재측정 전까지 ADR 상태는 `proposed`다. 측정 데이터는 개발 DB `marine`에 넣지 않는다(audit_log는 트리거로 지울 수 없다) — 끝나면 임시 DB를 `DROP`한다.
ADR 파일은 `0042-hot-path-indexes.md`로 옮기고 'ADR-0040' 인용 9곳(코드·시험 8곳 + ADR 파일 제목 1곳)을 0042로 고친다(0040은 9번이 쓴다). 마이그레이션 왕복(`tools/migration_check.py`)과 전체 시험을 확인한다. 마이그레이션 번호는 현재 머리를 확인해 이어 붙인다(0024가 이 브랜치 몫).
숫자는 지어내지 않는다.

### D. 점검 결과 처리·검토

**11) 계약 대비 구현 점검 — 남은 25건 (L)** — `docs/review-2026-11-05/contract-sweep-findings.md`의 27건 중 1번(backup-runs)·5번(backfill)은 `dedupe_active_only`로 이미 고쳤다(`HANDOFF.md` 0장 -1ab). 14·20번(멱등 키 동시 요청)과 4번의 응답 뒤 롤백 부분은 5번 몫이라 건드리지 않는다. 4번의 null 거부(`PATCH /v1/external-sources/{id}`의 `SourcePatch`가 명시적 null을 400으로 거부해야 함, `backend/app/api/external.py`)는 5번과 무관하므로 이 항목에서 고친다.
발견자는 재현했지만 검증자 검토를 거치지 않았다. 한 번에 5건 이내로 묶어 중간(medium) 심각도(2·3·6·7·8·15·16·17·18번)부터, 각각 먼저 재현해 진짜인지 확인하고 진짜면 고친다. 계약 문구가 틀렸다면 계약부터(26번은 ADR-0029 결정 4가 정본이라 계약 문구를 고친다).
특히 '기기가 다른 기관 장비를 찾을 때 교차 접근 감사 미기록'(6·16·17번, security.md 위반), 형식 오류가 500인 것(7·8번: `PATCH /v1/events/{id}`, `POST /v1/observations/corrections`), 긴급 열람 본인 승인 거부 미감사(15번).
고치려면 domain.md 코드값·권한 표를 바꿔야 하는 건은 구현하지 말고 `HANDOFF.md` 5장에 올린다. 점검하지 못한 두 묶음(`datasets·sharing·deid·assets`, `reports·records·contracts·uploads`, 작업 약 70개)은 상한이 없는 일이라 **맨 마지막, 사용량이 남을 때만** 같은 방식
(계약 설명 ↔ 구현 비교 → 재현 → 수정)으로 한다.

**12) 독립 검토를 못 받은 병합분 검토 (L)** — 웹훅 전송 격리(ADR-0041), 구간 클립 보안 보완(ADR-0033 갱신), 기기 토큰 범위 축소, 계약 검증기, XLSX 수식 방어, 업로드 정리 SAVEPOINT. 문제를 찾으면 재현 시험부터 쓰고 고친다.
**기기 토큰 범위 축소**(`Permission_Table` 'device' 표시 + Tenant_Guard 기기 범위)는 서버 쪽 시험(`backend/tests/integration/test_device_reach.py`, `test_ingest.py`·`test_agents.py`의 기기 토큰 부분)은 있지만, **실제 Go 에이전트를 실제 서버에 붙여 에이전트 경로 전체(정책·매핑·하트비트·업로드·관측값·이벤트·영수증)를 지나가 보는 시험은 없다** — `tools/go_acceptance.py`는 `go test` 결과를 수용 보고서로 옮길 뿐 실제 서버와 도는 시험이 아니다.
직접 확인한다: `make up && make seed` → 등록 코드 발급(`docker compose --env-file .env -f deploy/compose/docker-compose.yml run --rm migrate python -m app.cli enrollment-code -h`로 인자 확인) → `make agent-build` →
`agent/bin/marine-agent enroll --server http://localhost:8000 --code <코드> --watch-root <합성 폴더> --dir <임시 폴더>`(`agent/README.md`) → `run`으로 합성 mp4 한 개를 올려 `GET /v1/me`·정책·매핑·하트비트·업로드·관측값·이벤트·영수증이
403/404 없이 지나가는지 본다. 막히는 경로가 있으면 `backend/app/domain/permissions.py`의 `device=True` 목록을 고치기 전에 재현 시험부터 쓴다. 자동화할 수 있으면 시험으로 추가한다.

### E. 모든 라우터를 건드리는 변경 — 마지막

**5) 검토 2 #4 — 요청 커밋이 응답 뒤에 일어남 (L)** — `get_db`(yield 의존성)의 종료가 응답 전송 뒤라 롤백된 작업에 2xx를 준다(같은 `Idempotency-Key` 동시 요청이 둘 다 201, 두 번째는 실제로 없음).
`Depends(get_db)`가 21개 파일 151곳이라 **다른 항목이 모두 합쳐진 뒤 한 번에** 고친다. 이 wip는 rebase하지 않는다 — 첫 커밋 `5220f6b`(라우터 21개를 고친 시도)가 `backend/app/api/webhooks.py`에서 충돌하고 둘째 커밋이 그 구현을 되돌려 순 변경은 시험 파일 2개뿐이다
(되돌려진 첫 시도는 `git show 5220f6b`로 볼 수 있다). 시작:

    git switch -c gpt/05-commit-before-response --no-track origin/fix/backup-runtime-pg16
    git checkout origin/wip/commit-before-response-tests -- backend/tests/integration/test_commit_before_response.py backend/tests/unit/test_db_dependency_scope.py

방법: `Depends(get_db, scope="function")`(설치된 FastAPI 0.162.0이 지원한다 — 소스로 확인) 또는 공용 commit 도우미. 커밋 시점의 DB 오류도 problem+json(409 등)으로 매핑되게 유지한다.
멱등 키는 `INSERT … ON CONFLICT`로 먼저 선점한다(`services/idempotency.py`). 지금 `backend/app`에는 `StreamingResponse`·`text/event-stream`이 없고 보고서 내보내기(`reports.py`)·프레임(`deid.py`)은 `Response(bytes)`다 —
이후 스트리밍이 생길 때를 위한 시험만 남긴다(HANDOFF의 'SSE'는 스토리지 서버측 암호화로 이것과 무관하다). 구간 클립 POST만 이미 202 전에 커밋한다. 계약 점검 발견 14·20번과 4번의 응답 뒤 롤백 부분이 이 항목에 속한다. 전체 시험을 여러 번 돌려야 한다.

## [4] 하지 말 것 / 멈추고 사람에게 넘길 것

- **사람이 정할 일은 구현하지 말고** 선택지·장단점·추천만 써서 `HANDOFF.md` 5장의 해당 행에 보강한다(이미 있는 행을 고치고, 5장에 없는 '정답 데이터 평가 기준' 등만 새 행으로 추가한다):
  운영자의 원본 열람 범위(운영자 기관 자산·원 관측값·타 기관 보고서 초안), 장비 재등록 방식 변경, 웹 프록시 뒤 등록 한도 키, 프록시 전용 망의 웹훅, 구성원 관리 Keycloak 연동, 운영 메신저, 탐지·임베딩 모델, HWPX 실제 양식, 정답 데이터 평가 기준 등.
- 하드웨어·현장·외부 확인이 필요한 일(엣지 15번, NGSI-LD 16번, 실제 기관 Open API, 10GB 끊고 재개, 운영 실측)은 시작하지 않는다.
- git: `--force`·`--force-with-lease`·`+ref` push는 모두 금지. `wip/*`·`main`·`fix/backup-runtime-pg16`의 기록을 고치지 않는다. 브랜치 삭제(`git branch -D`, `git push --delete`)·`git reset --hard`·`git clean`·`git checkout -- .`·`git stash drop`·`docker compose down -v`는 하지 않는다.
  `main`에 합치거나 PR을 만들지 않는다(요청받았을 때만). `.env`·비밀값을 커밋하지 않는다(`backend/.venv/bin/python tools/check_secrets.py`).
- 개발 DB `marine`에는 측정·실험 데이터를 넣지 않는다(감사 로그는 트리거로 지울 수 없다). 임시 DB를 쓰고 끝나면 지운다.

## [5] 작업 방식·체크포인트·기록

- 항목마다: ① 수정 전 코드에서 실패하는 회귀 시험을 먼저 쓴다(고친 코드를 잠깐 되돌려 실패를 확인) ② API가 바뀌면 계약 먼저 ③ 최소 변경(관련 없는 정리 금지) ④ 관련 시험 → 단계 끝에 백엔드 전체 시험
  ⑤ 스펙 `tasks.md` 줄(요구사항 번호) ⑥ 새 설계 결정이면 ADR(필수 — 번호는 커밋 직전에 `ls docs/adr`와 `git ls-tree -r --name-only origin/fix/backup-runtime-pg16 docs/adr`로 다시 확인한다: 0040 = 9번, 0042 = 10번, 그 밖의 새 결정은 0043부터)
  ⑦ 커밋(한국어, 무엇·왜·시험 결과). 새 마이그레이션이 필요하면 0024는 10번 몫이므로 그 항목을 먼저 합친 뒤 이어 붙인다.
- **사용량 한도는 예고 없이 끊긴다.** 그래서 ① 항목을 시작하면 바로 `gpt/<NN>-<이름>` 브랜치를 만들어 push한다. ② 시험 결과가 한 번 나올 때마다(또는 30분마다) 'WIP(미완) <항목번호> …' 커밋을 `gpt/*`에 push한다 — 시험이 깨진 상태여도 된다.
  커밋 메시지에 '여기까지 한 것 / 다음에 할 것 / 실패 중인 시험 이름'을 적는다. ③ WIP는 `gpt/*`에만 올린다. 이미 push한 `gpt/*`는 rebase하지 않는다(최신 fix가 필요하면 `git merge origin/fix/backup-runtime-pg16`).
  ④ `fix/backup-runtime-pg16`에는 [1]의 모든 조건을 채우고 백엔드 전체 시험·계약/생성물 검사가 통과한 항목만 합친다:

      git switch fix/backup-runtime-pg16 && git pull --rebase origin fix/backup-runtime-pg16
      git merge --squash gpt/<NN>-<이름>      # 변경만 스테이징된다. 커밋은 아직 없다
      # HANDOFF.md를 아래 규칙대로 고치고, 백엔드 전체 시험·계약/생성물 검사를 한 번 더 확인한 뒤
      git commit -m "[지시서 <N>번] <무엇·왜·시험 결과, 한국어 한 건>"   # WIP 커밋 메시지의 내용을 모아 쓴다. WIP 커밋들은 fix 기록에 들어가지 않는다
      git push origin fix/backup-runtime-pg16
      # 푸시 뒤 git status가 깨끗한지, git log -1이 방금 커밋인지 확인한다

- **HANDOFF.md 갱신은 항목을 fix에 합치는 같은 커밋에:** ① 맨 위 `최종 갱신` 줄 ② 완료 표시 — '남은 일' 2절 표에 그 wip 행이 있으면(1·5·6·8·9·10번) 행 맨 앞에 '완료(날짜, -1ac)'를 적고, 행이 없는 항목(2·3·4·7·11·12·13번)은 1절·3절의 해당 줄 옆에 '(완료, 0장 -1ac)'를 붙이거나 남은 부분만 남기고 지운다 ③ `## 0. 상세 기록`에서 `-1ab.` 항목 바로 위에 `-1ac.`, `-1ad.` … 항목('지시서 N번 — 날짜·브랜치·한 일·시험 결과·남은 위험')을 쌓는다.
  끊기는 중이면 같은 내용을 WIP 커밋 메시지에 적어 둔다(HANDOFF가 `gpt/*`에만 있으면 다음 세션이 못 찾는다).
- 항목마다 보고: 바꾼 파일, 실행한 명령과 통과/실패 수(실패는 출력 그대로), 남은 것, 사람에게 묻고 싶은 것. 사용량이 남으면 마지막에 [2]의 기준선을 다시 돌려 보고한다.

## [6] 멈춤·건너뜀 규칙

- '멈춘다'는 **그 항목(또는 그 일부)만** 멈추고, 사유와 제안을 `HANDOFF.md` 5장에 적은 뒤 다음 항목으로 넘어간다는 뜻이다. 세션 전체를 끝내는 경우는 둘뿐이다: ① 기준선의 단위·통합 시험이 코드 때문에 실패한다 ② 이 문서가 금지한 일을 해야만 진행된다.
  어느 경우든 WIP를 `gpt/*`에 올리고 실패 출력을 그대로 보고한다.
- 멈춘 항목의 사유·제안·질문을 `HANDOFF.md`에 적는 수정은 코드와 섞지 않은 **문서만의 별도 커밋**으로, 항목이 미완이어도 fix에 바로 push해도 된다(`git switch fix/backup-runtime-pg16 && git pull --rebase origin fix/backup-runtime-pg16` → 문서 수정 → `git commit -m "[HANDOFF] <N>번 중단 사유·제안"` → `git push origin fix/backup-runtime-pg16`). 그래야 다음 세션이 찾는다.
- 같은 원인의 시험 실패를 두 번 고쳐 보고도 못 고치면 그 항목을 WIP로 올리고 다음 항목으로 간다.
- 환경 한계는 코드 실패가 아니다: 네트워크 없음(pip-audit·license-check·uv·npm·`go run …@버전`), Docker·ffmpeg·pg_dump 16 없음, 샌드박스 거부, Docker/PostgreSQL이 꺼져 시험이 한꺼번에 `connection refused`로 죽는 경우.
  막힌 명령과 오류를 그대로 적어 '미실행'으로 보고하고 그 도구가 필요 없는 항목으로 넘어간다. 샌드박스·권한을 우회하려 하지 않는다.
- 시험이 간헐적으로 실패하면(웹훅 시간 단언이 있는 시험은 부하에 민감할 수 있다) 한 번 다시 돌려 보되, 통과하더라도 '간헐 실패 1회(시험 이름·출력)'를 보고에 적는다. 그래도 실패하면 숨기지 말고 보고한다.
- 사용량이 모자라면 최종 기준선을 포기해도 되지만, 그 사실과 마지막으로 통과한 시험·실패한 시험을 보고에 적는다.
