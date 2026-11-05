# ADR-0023: 공급망 검사와 릴리스 게이트, 실제 브라우저 업로드 재개 시험

- 상태: accepted
- 날짜: 2026-11-05
- 관련: 17번 요구사항 1·4·6, 09번 기존 업로드 계약, T01-h·T02-g

## 맥락

기존 CI는 React Router 런타임 HIGH 3건과 SBOM 라이선스 표현 문제로 실패했다.
릴리스 도구는 시험 DB·SBOM이 없거나 시험을 생략해도 과거 수용 결과로 통과할 수 있었다.
웹 재개는 자동 시험이 없었다. 실제 브라우저 시험을 추가하자 첫 PUT가 서버의 5 MiB 파트 대신
파일 전체 12,320,010바이트를 보내고, 재개 완료 요청이 400으로 실패했다.

## 결정

1. 웹 의존성을 호환 가능한 수정판으로 갱신한다: React Router DOM 7.18.5, Vite 6.4.5,
   ESLint·@eslint/js 9.39.5. 기존 React 18과 선언형 라우팅 소스를 유지한다.
   Playwright Core 1.64.1(Apache-2.0, 기존 채택 도구)을 개발 의존성으로 명시한다.
   새 런타임 전이 의존성 cookie·set-cookie-parser는 MIT다.
   근거: [Router 리다이렉트](https://github.com/remix-run/react-router/security/advisories/GHSA-wrjc-x8rr-h8h6),
   [Router SSR](https://github.com/remix-run/react-router/security/advisories/GHSA-337j-9hxr-rhxg),
   [추가 RSC 수정](https://github.com/remix-run/react-router/security/advisories/GHSA-qwww-vcr4-c8h2),
   [Vite 수정](https://github.com/advisories/GHSA-587h-7wmj-h5pm).
2. PyPI 이름은 [공식 정규화](https://packaging.python.org/en/latest/specifications/name-normalization/)를 따른다.
   SPDX는 [표준 문법](https://spdx.github.io/spdx-spec/v2.3.1/SPDX-license-expressions/)의 괄호와 WITH > AND > OR를
   보존한다. 기존 허용 목록과 사람의 승인 상태는 바꾸지 않는다. 예외는 패키지·생태계·실제 라이선스 식이 일치해야 한다.
   LicenseRef는 추출 원문으로 해석하며 이름·링크만으로 허용하지 않는다.
3. Syft 1.18.1 실측에서 pg8000의 추출 원문은 `BSD 3-Clause License`, s3transfer는 `Apache License 2.0`이므로
   명확한 별칭을 해석한다. python-dateutil의 `Dual License`에는 선택 근거와 seeAlsos가 없어 미상으로 남긴다.
   자체 marine-platform을 포함한 미상·미승인 예외는 개발 검사에서 경고, `--strict` 릴리스 검사에서 실패다.
   승인 대기 항목과 라이선스 확인 업무를 코드 수정만으로 승인 처리하지 않는다.
4. 게이트는 현재 실행의 필수 검사·시험·이미지·SBOM을 요구한다. 도구·DB·DAST 대상이 없거나 `--no-tests`면
   릴리스를 허용하지 않는다. 웹은 `npm ci` 후 빌드·검사하며 설치 실패 시 과거 node_modules를 검사하지 않는다.
   마이그레이션은 무작위 임시 DB에서 head → base → head → base로 실행하고 그 DB만 정리한다.
   수용 실행기는 수용 ID가 없는 시험·실행기 실패도 종료 코드에 반영한다. 실패해도 보고서는 생성한다.
5. API와 웹에 ZAP baseline을 실행한다. `-I`의 종료 코드만 믿지 않고 새 JSON의 riskcode를 검사하여 HIGH를 차단한다.
   보고서 누락·형식 손상·미상 위험 등급도 실패다. 낮은 위험 경고는 보관하며 T01-h의 HIGH 0 기준과 구분한다.
   이는 수동 탐색 기반 검사이며 인증된 모든 경로의 안전을 증명하지 않는다. BOLA는 기존 T01 시험으로 검사한다.
   근거: [baseline 동작](https://www.zaproxy.org/docs/docker/baseline-scan/),
   [Traditional JSON 형식](https://www.zaproxy.org/docs/desktop/addons/report-generation/report-traditional-json/).
6. T02-g는 실제 React·FastAPI·PostgreSQL과 기존 모의 S3/합성 서명 IdP 어댑터로 시험한다.
   탭 종료 후 같은 upload_id·asset_id, 완료된 파트 미재전송, 남은 파트 전송, 서버 전체 해시·크기를 검증한다.
   제품 서버에 인증 우회 설정을 추가하지 않는다. Uppy 4.1.3은 getChunkSize에 file.data(Blob)를 전달하므로
   Blob별 WeakMap에 서버 part_size를 연결한다. 임의의 기본 파트 크기로 대신하지 않는다.

## 결과

런타임 npm 취약점은 0건이며 HIGH·CRITICAL 개발 의존성도 0건이다. minimatch 관련 MODERATE 11건은 남는다.
허용 목록 밖 라이선스인 minimatch 11로 강제 교체하지 않는다. Trivy 이미지·소스 HIGH/CRITICAL 0건,
합성 API·빌드 웹의 ZAP HIGH 0건을 확인했다. 낮은 위험 경고와 운영 환경 검증은 남는다.
실제 T02-g는 5,242,880바이트 첫 파트를 보존한 뒤 2·3번 파트만 보내고 완료 202·READY_PRIVATE·해시 일치로 통과했다.

API·JSON·도메인 코드값·DB 구조는 바꾸지 않아 새 마이그레이션과 down은 해당 없음이다.
릴리스는 미상·승인 대기 라이선스 때문에 차단된다. 변경 후 원격 GitHub Actions는 아직 실행하지 않았다.
