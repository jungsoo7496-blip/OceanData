# ADR-0039: 접근성 자동 검사를 실제 스택 없이 CI에서 돌린다

- 상태: accepted
- 날짜: 2026-11-05
- 관련: 09번 요구사항 11.2·작업 15, security.md(KWCAG), ADR-0016(웹 의존성·Uppy 알려진 문제), ADR-0023(실제 브라우저 업로드 재개 시험)

## 맥락

요구사항 11.2는 자동 접근성 검사를 CI에 넣으라고 한다. 지금까지의 axe 검사(`web/e2e/portal.mjs`)는 `make up && make seed`로 띄운
Keycloak·API·DB에 로그인해야 해서 CI에 넣지 못했고(작업 15에 '실제 스택이 필요 → 17번과 함께'로 미뤄 둠), 화면 4개만 보며
critical만 실패로 쳤다. 접근성은 그려진 화면의 성질이라 서버가 실제일 필요는 없다. 대신 화면이 실제로 그려졌는지와 응답 모양이 계약과
같은지는 보장해야 한다 (빈 화면이나 깨진 응답은 검사를 그냥 통과한다).

## 결정

1. `web/e2e/a11y-mock.mjs`: 빌드한 `dist/`를 `vite preview`로 띄우고, OIDC 사용자는 oidc-client-ts의 세션 저장소 키에 넣고,
   `/v1/**`은 Playwright 요청 라우팅으로 합성 응답을 준다. 합성 응답(`web/e2e/fixtures/api.mjs`)은 계약 생성 타입
   (`src/contract/api.gen.ts`)으로 `npm run typecheck`가 검사한다 (JSDoc + `e2e/tsconfig.json`). 계약에 속성 없는 object로만 정한
   필드(생성 타입 `Record<string, never>`)와 서버가 더 주는 필드만 이름 붙은 도우미(`free`, `withExtra`)로 표시하고 건너뛴다.
2. 로그인 전과 역할 6개의 메뉴 화면, 상세 화면, 열린 대화상자·서랍 42개 상태를 데스크톱 1280px·휴대폰 390px에서 검사한다.
   화면마다 확인 문구가 보여야 하고, 합성 응답 없는 `/v1` 호출·외부 요청·페이지 예외·콘솔 오류는 실패다.
3. 실패 기준: axe `wcag2a`·`wcag2aa`·`wcag21a`·`wcag21aa`의 영향도 serious·critical (KWCAG 2.2가 WCAG 2.1을 바탕으로 하므로 2.1 AA까지).
   moderate·minor는 출력만 한다. 휴대폰 폭 `scrollWidth > innerWidth + 1`과 화면 밖 대화상자, Tab으로 간 링크의 초점 표시 없음도 실패다.
4. 예외는 화면 코드로 고칠 수 없는 외부 라이브러리 노드만, 규칙·요소·속성을 좁혀 뺀다 (뺀 수 출력):
   rc-select 14.16 바깥 div의 `aria-required`(입력 role=combobox에도 붙는지 따로 확인), Uppy Dashboard 4.1.3 inline의 `aria-modal="false"`.
   ADR-0016의 `.uppy-Dashboard` 전체 제외는 이 두 번째로 좁혔다 (Uppy 안내 문구 대비는 CSS로 고쳤다).
5. 검사로 찾은 문제는 화면에서 고친다: 링크 초점 윤곽선, 빈 표 문구·위험 버튼·마우스 올림·선택 상자 안내 문구 대비(테마 토큰·CSS),
   머리글 줄바꿈과 body 여백(390px 가로 넘침, 대화상자 열 때 8px 넘침), 운영 콘솔 사용률 막대 이름, 지표 근거를 키보드로 열기, 문장 속 링크 밑줄.
6. 좁은 화면의 표는 표 안에서 가로로 스크롤한다 (자료표는 WCAG 1.4.10 예외). 링크·버튼이 없는 표도 키보드로 스크롤하도록
   `src/lib/scrollRegions.ts`가 실제로 넘치는 표 영역에만 tabindex=0·role=region·이름을 붙인다. AntD Table이 그 영역에 속성을 줄 방법을 주지 않아서다.

버린 대안:
- 실제 스택으로 CI 검사: compose·Keycloak·시드가 필요해 느리고, 화면별 상태(대화상자·빈 목록·오류)를 만들기 어렵다. 실제 연동은 `portal.mjs`·T02-g가 맡는다.
- Lighthouse·pa11y 같은 새 도구: 이미 채택한 `@axe-core/playwright`·`playwright-core`로 충분하다. 새 의존성은 없다.
- AntD 가로 메뉴의 '…' 접기: 숨긴 항목에 aria-hidden만 붙고 안의 링크는 Tab으로 갈 수 있어(axe aria-hidden-focus) 쓰지 않는다.
- 표 글자를 한 글자씩 끊어 화면 폭에 맞추기: 넘침을 다 없애지 못하고(태그·버튼·고정 폭) 읽기 어렵다.

## 결과

- CI `web` 작업이 빌드 뒤 Chromium을 받아 `npm run test:a11y`를 돌린다 (4개 동시, 로컬 약 1분 30초). 로컬은 `make web-a11y`.
- 2026-11-05 84개 상태 통과 (Chrome for Testing 153 전체 실행 파일과 headless shell 둘 다).
- 자동 검사는 KWCAG 항목의 일부만 본다. 화면 낭독기 사용감, 대체 텍스트·안내 문구의 적절성은 사람이 확인해야 한다.
  합성 응답은 계약 모양만 보장하고 서버 동작은 대신하지 않는다.
- 되돌리기: CI의 두 단계(Chromium 설치·접근성 검사)를 빼면 된다. 화면 수정은 그대로 두어도 된다.
