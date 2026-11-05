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
기존 개발 환경의 Python 가상환경을 쓰는 경우 `make test-web-e2e`로 같은 시험을 실행한다.

`tools/web_acceptance.py`는 고유한 Docker Compose 프로젝트와 사용하지 않는 루프백 포트를 선택한다.
끝나면 그 프로젝트의 컨테이너·DB 볼륨·네트워크만 정리한다. 다른 개발 서비스는 변경하지 않는다.
서명 토큰 파일은 실행이 끝나면 삭제한다. 로그는 `var/marine-web-e2e-*/run.log`에 남고,
결과는 Python·Go 수용시험과 같은 JSON Lines(`nodeid`, `covers`, `outcome`, `log`, `reproduce`)로 이어 쓴다.
환경 준비나 시험이 실패하면 실패 행을 기록하고 종료 코드 1을 반환한다.

기존 `portal.mjs`는 `make up && make seed` 환경의 로그인·대시보드·업로드·자산 접근성 점검용이다.
그 시험은 T02-g 재개 결과를 대신하지 않는다.

구현 근거: [Playwright 요청 라우팅](https://playwright.dev/docs/api/class-browsercontext#browser-context-route),
[브라우저 채널](https://playwright.dev/docs/api/class-browsertype#browser-type-launch-option-channel),
[Vite JavaScript API](https://vite.dev/guide/api-javascript#createserver).
