# 설계: 09 워크스페이스 포털

## 구조 (`web/`)
React 18 + TS + Vite, Ant Design 5(ko_KR, 대비 강화 테마), TanStack Query, react-router, oidc-client-ts(PKCE), Uppy(@uppy/aws-s3), hash-wasm. 의존성 근거 ADR-0016.
- 같은 출처: nginx(`web/nginx.conf`)와 Vite 개발 서버가 `/v1`을 API로 프록시한다. 저장소 직송은 사전 서명 URL(브라우저 → S3, 운영 스토리지 CORS 설정 필요).
- `src/lib/api.ts`(Bearer, problem+json → ApiError), `src/lib/problems.ts`(오류 코드별 원인·다음 행동), `src/lib/format.ts`(KST·L0~L4 라벨).
- 접근성(요구사항 11.2): `src/main.tsx` 테마 대비(파랑·마우스 올림·오류 빨강·보조 글자·안내 문구), `src/a11y.css`(대비, 링크 키보드 초점, 문장 속 링크 밑줄,
  재배치: body 여백 0·머리글 줄바꿈·표는 표 안에서 가로 스크롤). 머리글 메뉴는 넘치면 줄바꿈(`Menu disabledOverflow`) — AntD '…' 접기는 숨긴 링크를 Tab에 남긴다.
  `src/lib/scrollRegions.ts`: 실제로 넘치는 표 영역에만 tabindex=0·role=region·이름(카드·탭·대화상자 제목)을 붙여 키보드로 스크롤하게 한다.

## 화면
| 경로 | 역할 | 내용 | 요구사항 |
|---|---|---|---|
| / | 모두 | 역할별 첫 화면(ORG→/workspace, 운영자→/operator, 관리기관→/program), 역할별 메뉴 | 1 |
| /workspace | ORG | 회차 수·사용량/쿼터·처리 대기·격리, 수집기 상태(하트비트·마지막 수집), 최근 격리, 기능 배너(GPU 없음 등) | 2 |
| /devices | ORG | 장비 목록·등록(ORG_ADMIN), 등록 코드 발급(1회 표시)·자격증명 폐기, 수집 방식·현장 사본, 유일본 경고, 설치 안내 | 4 |
| /sessions, /sessions/:id | ORG | 회차 목록·생성(KST 입력 → UTC), 회차 미상 자산 회차 지정(재검증), 회차 타임라인(자산·이벤트·관측값·환경 정보), 회차 수정(제목·기간·장소·메모·장비) | 5.1~5.3 |
| /assets, /assets/:id | ORG | 필터(회차·장비·유형·변형·품질·상태·등급), L0~L4·역할, "촬영시각 미상"·"미검증" 구분, 상세·계보, 프록시 재생(단기 URL), 원본 내려받기(감사), 구간 클립 내보내기(재생 위치·숫자로 구간 → 작업 등록, 작업 목록 5초마다 갱신) | 6, 7.1~7.3 |
| /upload | ORG | 원천 장비·변형·회차 선택, 브라우저 SHA-256 → POST /v1/uploads → Uppy 파트 전송 → complete. 같은 파일 재선택 시 서버가 진행 중 세션을 주고 listParts로 받은 파트를 건너뜀. 결과 표에 오류별 원인·다음 행동 | 8 |
| /settings | ORG_ADMIN | 연계 카드 목록·등록·새 버전(양식 밖 분야는 이전 버전 값 유지), 카드별 확인 필요 개수, '정책 초안' 마법사(카드 확인: 확인 필요 항목·난이도 '기준 미정 — 확인 필요'·카드 장비 항목 → 장비 선택: 수집기 매핑 없는 장비는 고를 수 없음 → 초안: 항목별 카드 값/입력함/확인 필요, 지금 정책과 비교), 장비 정책 초안 목록 (`pages/Onboarding.tsx`) | 3.1 일부, 3.2~3.4 |
| /program | PROGRAM_VIEWER | 담당 사업·회차, L1 자산 메타데이터만 (바이너리 없음) | 9 |
| /operator | PLATFORM_OPERATOR | 기관 사용량, 정책 편집(기관 쿼터·악성코드 검사, 보존 정책, 작업 우선순위 — 다중 인증·사유·전후 감사), 보존 dry-run, 전 기관 격리 목록, 감사 로그, 약정·긴급 열람·백업·용량 예측·모델 등록부, 장비 정책 초안 검토(상태별 목록 → 지금 정책과 비교·적용을 막는 조건 → 사유 → 적용/반려, 다중 인증) | 3.3, 10.1 일부, 10.2 |

## 서버 변경
`POST /v1/assets/{id}/download-url`(계약 추가): 자기 기관 ORG 역할만, 처리 끝난 자산만, 격리 불가, 발급 감사. 관리기관·운영자 403.

`POST·GET /v1/assets/{id}/clips`(계약 추가, ADR-0033): 자기 기관 ORG 역할만. 요청은 원 영상·구간을 검사하고 작업 `clip_export`를
등록(202)하며, 작업은 입력 앞 -ss + libx264 재인코딩으로 프레임 단위로 잘라 파생 자산(CLIP, 계보 relation=CLIP,
`provenance.clip_range_s`, 옮긴 촬영 시각)을 만든다. 원 영상 격리·삭제와 인코딩 실패는 재시도하지 않고 바로 FAILED(코드 표시).
새 작업은 기관 쿼터에 여유가 있어야 하고(409 QUOTA_EXCEEDED, 업로드와 같은 규칙) 기관의 대기·실행 중 클립 작업이
`CLIP_MAX_PENDING_PER_ORG` 미만이어야 한다(429 RATE_LIMITED + Retry-After). 작업 행과 감사(CLIP_EXPORT_REQUESTED)는 요청 트랜잭션
하나로 202 전에 커밋한다. 작업은 클립을 올린 뒤 원 영상 행을 잠그고 살아 있는지·쿼터를 다시 확인해 기록하고(아니면 FAILED
`SOURCE_UNAVAILABLE`·`QUOTA_EXCEEDED`, 올린 객체 삭제), 삭제는 원 자산 행을 잠근 뒤 파생본을 찾는다(ADR-0033 갱신).

장비 정책 초안(계약 추가, ADR-0037, 요구사항 3.2~3.4): 표 `policy_drafts`(마이그레이션 0022, ID `pdr_`, 장비마다 DRAFT 하나 —
부분 고유 색인), 상태 DRAFT → APPLIED | REJECTED, 항목 일곱 개(collection_mode, allowed_data_roles, local_copy_policy,
local_retention_days, expected_interval_s, transfer_windows=수집기 전체, source_timezone)마다 FROM_CARD(카드 위치 함께)·ENTERED·
NEEDS_CONFIRMATION. `GET/POST /v1/policy-drafts`(ORG_ADMIN 자기 기관·운영자, 카드의 구조화된 값 세 개만 미리 채움, 자유 기록은
쓰지 않음, 수집기 매핑 없는 장비·다른 장비의 카드 422), `GET/PATCH /v1/policy-drafts/{id}`(revision 일치할 때만, 바뀐 항목 전후
감사), `POST …/apply`(운영자 + 다중 인증 `device_policy:write`, 검토한 revision과 미리보기에서 본 수집기 정책 버전 `expected_policy_version` — 둘 중 하나라도 바뀌었으면 409, 지금 정책에서 그 장비 항목만 바꾸고 확인 필요
항목은 유지 → `services/device_policy.store_policy`(관리 명령 set-policy와 같은 계약·매핑·같은 기관 카드 검사, 수집기 행 잠금)로
새 버전 `pv-<버전>` + 장비 기록 반영, 실패 시 422·아무것도 안 씀, 감사 POLICY_CHANGED 전후·POLICY_DRAFT_APPLIED),
`POST …/reject`(운영자 + 다중 인증, 사유 필수). 상세 응답 `preview`는 지금 정책·적용 후 값·바뀌는 항목·막는 조건을 조회 때 계산.
연계 카드 응답에 `open_items`(부록 A 분야 중 비어 있음 / '확인 필요'·현장 사본 UNKNOWN + 위치)·`difficulty_grade`(기준 미정 —
grade=null, criteria_defined=false).

`PATCH /v1/sessions/{id}`(계약 추가): 자기 기관 ORG 역할만. 보낸 항목만 바꾸고 ends_at·site·note는 null로 비운다. label(에이전트 폴더 매칭)과
project_id(자산 소속)는 바꾸지 않는다. 바뀐 항목의 전후 값은 감사 SESSION_UPDATED(메모 본문은 길이만).

## 시험
`web/e2e/portal.mjs`: 실제 스택에서 Keycloak 로그인 → 대시보드 → 업로드 → 재선택(이미 등록) → 자산 목록, axe(WCAG 2 A·AA) critical 0건 확인. 2026-11-04 통과 (프록시 재생 포함 수동 확인).

`web/e2e/a11y-mock.mjs` (`npm run test:a11y`, `make web-a11y`, CI web 작업 — ADR-0039): 실제 스택 없이 빌드한 웹을 띄우고 OIDC 사용자는 세션 저장소,
`/v1`은 계약 모양 합성 응답(`web/e2e/fixtures/api.mjs`, 계약 생성 타입으로 typecheck)으로 흉내 낸다. 로그인 전·역할 6개의 메뉴 화면·상세 화면·열린 대화상자
42개 상태를 데스크톱(1280px)·휴대폰(390px)에서 검사한다. 실패 기준: axe WCAG 2.0·2.1 A·AA 영향도 serious·critical 위반(moderate·minor는 출력만),
휴대폰 폭 가로 넘침(scrollWidth > innerWidth + 1, 대화상자가 화면 밖), Tab 링크 초점 표시 없음, 화면 확인 문구 없음·페이지 예외·콘솔 오류·합성 응답 없는 호출·외부 요청.
외부 라이브러리 예외는 노드 단위 2건만 (rc-select 바깥 div의 aria-required — 입력 쪽 aria-required는 따로 확인, Uppy inline 대시보드의 aria-modal="false"). 2026-11-05 84개 통과.

온보딩(작업 3): `tests/unit/test_onboarding_rules.py`(확인 필요 항목, 난이도 기준 미정, 자유 기록이 미리 채우기에 쓰이지 않음·확인 필요
항목은 정책을 바꾸지 않음 — 속성 기반), `tests/integration/test_policy_drafts.py`(미리 채우기·초안은 아무것도 안 바꿈·ORG_ADMIN 적용 불가·
다중 인증·revision 409·적용 결과 정책·장비 기록·감사 전후·저장본 유지·막는 조건 422·반려·입력 400·기관 격리), T01 교차 기관 사례.
화면은 Playwright(합성 응답, 데스크톱·390px)로 마법사 3단계·저장 요청 본문·운영자 검토·적용 요청 본문 확인, 단계마다 axe serious/critical 0건 (2026-11-05).
