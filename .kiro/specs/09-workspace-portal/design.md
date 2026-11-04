# 설계: 09 워크스페이스 포털

## 구조 (`web/`)
React 18 + TS + Vite, Ant Design 5(ko_KR, 대비 강화 테마), TanStack Query, react-router, oidc-client-ts(PKCE), Uppy(@uppy/aws-s3), hash-wasm. 의존성 근거 ADR-0016.
- 같은 출처: nginx(`web/nginx.conf`)와 Vite 개발 서버가 `/v1`을 API로 프록시한다. 저장소 직송은 사전 서명 URL(브라우저 → S3, 운영 스토리지 CORS 설정 필요).
- `src/lib/api.ts`(Bearer, problem+json → ApiError), `src/lib/problems.ts`(오류 코드별 원인·다음 행동), `src/lib/format.ts`(KST·L0~L4 라벨).

## 화면
| 경로 | 역할 | 내용 | 요구사항 |
|---|---|---|---|
| / | 모두 | 역할별 첫 화면(ORG→/workspace, 운영자→/operator, 관리기관→/program), 역할별 메뉴 | 1 |
| /workspace | ORG | 회차 수·사용량/쿼터·처리 대기·격리, 수집기 상태(하트비트·마지막 수집), 최근 격리, 기능 배너(GPU 없음 등) | 2 |
| /devices | ORG | 장비 목록·등록(ORG_ADMIN), 등록 코드 발급(1회 표시)·자격증명 폐기, 수집 방식·현장 사본, 유일본 경고, 설치 안내 | 4 |
| /sessions | ORG | 회차 목록·생성(KST 입력 → UTC), 회차 미상 자산 회차 지정(재검증) | 5.1, 5.3 |
| /assets, /assets/:id | ORG | 필터(회차·장비·유형·변형·품질·상태·등급), L0~L4·역할, "촬영시각 미상"·"미검증" 구분, 상세·계보, 프록시 재생(단기 URL), 원본 내려받기(감사) | 6, 7.1, 7.3 |
| /upload | ORG | 원천 장비·변형·회차 선택, 브라우저 SHA-256 → POST /v1/uploads → Uppy 파트 전송 → complete. 같은 파일 재선택 시 서버가 진행 중 세션을 주고 listParts로 받은 파트를 건너뜀. 결과 표에 오류별 원인·다음 행동 | 8 |
| /program | PROGRAM_VIEWER | 담당 사업·회차, L1 자산 메타데이터만 (바이너리 없음) | 9 |
| /operator | PLATFORM_OPERATOR | 기관 사용량, 보존 dry-run, 전 기관 격리 목록, 감사 로그 | 10.1 일부 |

## 서버 변경
`POST /v1/assets/{id}/download-url`(계약 추가): 자기 기관 ORG 역할만, 처리 끝난 자산만, 격리 불가, 발급 감사. 관리기관·운영자 403.

## 시험
`web/e2e/portal.mjs`: 실제 스택에서 Keycloak 로그인 → 대시보드 → 업로드 → 재선택(이미 등록) → 자산 목록, axe(WCAG 2 A·AA) critical 0건 확인. 2026-11-04 통과 (프록시 재생 포함 수동 확인).
