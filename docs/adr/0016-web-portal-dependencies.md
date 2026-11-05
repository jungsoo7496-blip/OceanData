# ADR-0016: 웹 포털 의존성 (OIDC·라우팅·해시)

- 상태: 채택
- 날짜: 2026-11-04
- 관련: 09번, tech.md 2장(React + TS + Vite, TanStack Query, Ant Design ko_KR, Uppy S3 멀티파트), docs/OSS-CANDIDATES.md

## 결정
OSS-CANDIDATES의 채택 항목(Ant Design, TanStack Query, Uppy `@uppy/aws-s3`, `@axe-core/playwright`) 외에 아래를 더한다. 모두 tech.md 3장 허용 목록 라이선스다 (`tools/check_licenses.py`가 package-lock.json으로 확인).

| 패키지 | 라이선스 | 이유 |
|---|---|---|
| oidc-client-ts | Apache-2.0 | Keycloak 인가 코드 + PKCE 로그인·토큰 갱신. 직접 구현하지 않는다 |
| react-router-dom | MIT | 역할별 진입·화면 이동 (09번 요구사항 1) |
| hash-wasm | MIT | 브라우저에서 대용량 파일 SHA-256을 조각 단위로 계산 (전체를 메모리에 올리지 않음). 매니페스트에 필요 |
| dayjs | MIT | Ant Design 날짜 선택기 |

- Refine(OSS-CANDIDATES PoC)은 쓰지 않았다. 이번 범위의 화면이 CRUD보다 업무 흐름 위주라서다. 운영 콘솔의 정책 편집 화면을 만들 때 다시 검토한다.
- 폼은 Ant Design Form으로 통일했다 (React Hook Form PoC는 하지 않음).

## 알려진 문제
- Uppy 대시보드 내부 요소가 axe `aria-allowed-attr`(critical)에 걸린다. 외부 라이브러리 문제라 `web/e2e/portal.mjs`에서 `.uppy-Dashboard`를 검사에서 빼고 여기 기록한다. Uppy 판올림 때 다시 확인한다.
  - 2026-11-05: Uppy Dashboard 4.1.3에서 남은 것은 inline 대시보드 안쪽 div의 `aria-modal="false"`뿐이다. CI 접근성 검사(`web/e2e/a11y-mock.mjs`)는 그 노드만 빼고, 안내 문구 대비는 CSS로 고쳤다 (ADR-0039).
