# 웹 수용시험

`T02-g`는 실제 React 업로더에서 3개 이상 파트의 합성 CSV를 보내며 다음을 확인한다.

1. 첫 파트를 실제 S3 API에 저장하고 나머지 전송을 끊는다.
2. 탭을 닫은 뒤 서버에 저장된 파트가 그대로 있는지 조회한다.
3. 같은 브라우저의 새 탭에서 같은 파일을 다시 고른다.
4. 서버의 `upload_id`·`asset_id`가 같고, 이미 저장된 1번 파트는 다시 보내지 않는지 확인한다.
5. 완료 API·서버 무결성 검증·영수증의 전체 SHA-256과 크기를 확인한다.

브라우저의 업로드 결과나 API 응답을 성공으로 대체하지 않는다. PostgreSQL과 제품 FastAPI 서버를 실행하며,
외부 의존인 S3는 기존 시험 도구 moto, IdP는 시험 파일 안의 임시 RSA 서명 어댑터를 사용한다.
제품 코드에 인증 우회 플래그를 추가하지 않는다. OIDC 로그인 화면 자체는 이 시험의 범위가 아니다.

## 실행

Docker Compose v2, Node 22+, Python 3.12+가 필요하다.

```sh
cd web
npm ci
node node_modules/playwright-core/cli.js install chromium
cd ..
python tools/web_acceptance.py --out var/acceptance-results.jsonl
```

Linux CI에서 브라우저 시스템 라이브러리도 설치하려면 `install --with-deps chromium`을 쓴다.
설치된 Chrome을 사용하려면 `E2E_BROWSER_CHANNEL=chrome`을 지정한다.
브라우저 저장 위치는 `PLAYWRIGHT_BROWSERS_PATH`로 지정할 수 있다.
미리 받아 둔 다른 Chromium 실행 파일은 `E2E_BROWSER_EXECUTABLE`로 지정한다 (아래 접근성 검사의 `E2E_CHROMIUM_PATH`와 이름이 다르다).
기존 개발 환경의 Python 가상환경을 쓰는 경우 `make test-web-e2e`로 같은 시험을 실행한다.

`tools/web_acceptance.py`는 고유한 Docker Compose 프로젝트와 사용하지 않는 루프백 포트를 선택한다.
끝나면 그 프로젝트의 컨테이너·DB 볼륨·네트워크만 정리한다. 다른 개발 서비스는 변경하지 않는다.
서명 토큰 파일은 실행이 끝나면 삭제한다. 로그는 `var/marine-web-e2e-*/run.log`에 남고,
결과는 Python·Go 수용시험과 같은 JSON Lines(`nodeid`, `covers`, `outcome`, `log`, `reproduce`)로 이어 쓴다.
환경 준비나 시험이 실패하면 실패 행을 기록하고 종료 코드 1을 반환한다.

기존 `portal.mjs`는 `make up && make seed` 환경의 로그인·대시보드·업로드·자산 접근성 점검용이다.
그 시험은 T02-g 재개 결과를 대신하지 않는다. CI의 접근성 검사는 아래 `a11y-mock.mjs`다.

## 접근성 검사 (실제 스택 없이, 09번 작업 15·요구사항 11.2)

`a11y-mock.mjs`는 빌드한 웹(`dist/`)을 `vite preview`(127.0.0.1의 빈 포트 5201~5210)로 띄우고, Keycloak·API·DB 없이
역할마다 로그인한 상태를 흉내 내어 화면을 검사한다. CI `web` 작업이 빌드 뒤에 실행한다 (ADR-0039).

```sh
cd web
npm run build
node node_modules/playwright-core/cli.js install --only-shell chromium   # 처음 한 번 (헤드리스만 쓴다). CI는 --with-deps도
npm run test:a11y                                           # 저장소 루트에서는 make web-a11y (빌드 포함)
```

- 로그인: oidc-client-ts가 읽는 세션 저장소 키 `oidc.user:<authority>:<client_id>`에 시험용 사용자를 넣는다.
  빌드할 때 `VITE_OIDC_AUTHORITY`·`VITE_OIDC_CLIENT_ID`를 바꿨다면 같은 값을 주고 실행한다.
- API: `/v1/**` 요청은 `fixtures/api.mjs`의 합성 응답으로 돌려준다. 응답 모양은 계약 생성 타입(`src/contract/api.gen.ts`)으로
  `npm run typecheck`가 검사한다(`e2e/tsconfig.json`). 계약이 바뀌어 맞지 않으면 타입 검사가 실패한다.
  합성 응답이 없는 `/v1` 호출, 다른 출처로 나가는 요청, 페이지 예외·콘솔 오류도 실패로 센다.
- 화면: 로그인 전과 역할 6개(ORG_ADMIN·ORG_MEMBER·PLATFORM_OPERATOR·PROGRAM_VIEWER·REVIEWER·CONSUMER)가 메뉴로 가는 화면,
  상세 화면(자산 상세·구간 클립, 회차 타임라인, 회차 기록, 보고서 상세), 열린 상태(대화상자·서랍·프록시 재생·보존 dry-run 결과)
  46개를 데스크톱 1280×800과 휴대폰 390×844에서 본다 (92개). 화면마다 확인 문구가 보여야 검사한다 (빈 화면 통과 방지).
- 실패 기준 (하나라도 있으면 종료 코드 1):
  1. axe-core(`@axe-core/playwright`) `wcag2a`·`wcag2aa`·`wcag21a`·`wcag21aa` 규칙의 영향도 serious·critical 위반.
     moderate·minor는 '참고'로만 출력한다.
  2. 휴대폰 폭에서 `document.documentElement.scrollWidth > innerWidth + 1`, 또는 대화상자·서랍이 화면 밖으로 나감.
  3. Tab으로 옮겨 간 링크(`a[href]`)에 윤곽선·그림자가 없음 (axe가 보지 않는 초점 표시).
- 검사에서 빼는 것: 화면 코드로 고칠 수 없는 외부 라이브러리 문제만 규칙·요소·속성을 좁혀 노드 단위로 뺀다 (뺀 수를 출력한다).
  영역 전체를 빼지 않는다.
  - rc-select 바깥 div의 `aria-required`(`aria-allowed-attr`): AntD Form.Item의 필수 표시를 rc-select가 입력(role=combobox)과
    바깥 div 둘 다에 붙인다. 입력 쪽에 있는지는 검사가 따로 확인한다.
  - Uppy Dashboard(inline) 안쪽 div의 `aria-modal="false"`(`aria-allowed-attr`, ADR-0016).
- 합성 시험 패턴: `fixtures/testsrc.webm`(프록시 재생, data: URL이라 구간 이동이 된다)과 `fixtures/testsrc.jpg`(검수 표본·데이터셋 샘플)는
  FFmpeg testsrc로 만들었다 (tech.md HW-7).

  ```sh
  ffmpeg -f lavfi -i testsrc=size=160x90:rate=5 -t 2 -c:v libvpx-vp9 -b:v 20k -pix_fmt yuv420p -an -map_metadata -1 -fflags +bitexact testsrc.webm
  ffmpeg -f lavfi -i testsrc=size=160x90:rate=5 -frames:v 1 -q:v 10 -map_metadata -1 -fflags +bitexact testsrc.jpg
  ```
- 환경 변수: `E2E_CHROMIUM_PATH`(다른 Chromium 실행 파일), `E2E_BROWSER_CHANNEL=chrome`(설치된 Chrome),
  `A11Y_WORKERS`(동시에 돌릴 역할×화면 폭 묶음 수, 기본 4).
- 자동 검사는 KWCAG 항목의 일부만 본다. 화면 낭독기 사용감, 대체 텍스트·안내 문구의 적절성 같은 항목은 사람이 확인한다.

구현 근거: [Playwright 요청 라우팅](https://playwright.dev/docs/api/class-browsercontext#browser-context-route),
[브라우저 채널](https://playwright.dev/docs/api/class-browsertype#browser-type-launch-option-channel),
[Vite JavaScript API](https://vite.dev/guide/api-javascript#createserver),
[Vite preview](https://vite.dev/guide/api-javascript#preview),
[axe-core 태그](https://github.com/dequelabs/axe-core/blob/develop/doc/API.md#axe-core-tags).
