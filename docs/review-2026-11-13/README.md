# 2026-11-13 밤 작업 정리

병합을 정하기 전에 읽을 문서다. 2026-11-12~13 밤에 한 일, 사람이 정할 것, 남은 일을 모았다.
항목별 자세한 기록은 `HANDOFF.md` 0장(-1au~-1bm)과 각 ADR에 있다. 근거 자료는 두 가지다.
검토 2026-11-12는 `docs/review-2026-11-12/README.md`(아래 '검토 README'), 계약 점검은 같은 폴더의 `contract-sweep-findings.md`(아래 '발견 문서')다.

## 한눈에

- **검토 2026-11-12에서 남은 9건**(W1·W4·V5·C2·C4·V7·V8·V9·C7)을 항목 8개로 처리했다.
  고친 것 6건(W1·W4·V5·C4·V7·V9), 완화한 것 3건(C2·V8·C7)이다.
  V9의 관측 항목 위치 검사, C2의 상태 전이, W5는 사람 결정으로 남았다. C9(부모 업로드 만료 뒤 계보)는 오늘 밤 범위 밖이라 미완 그대로다.
- **계약 점검 발견 47건**(S01~S47)을 항목 11개로 처리했다.
  고친 것 38건, 계약 문구를 고친 것 6건(S04·S09·S15·S26·S33·S47), 고치지 않은 것 3건(S08·S13·S18)이다.
  고친 것 가운데 S45는 검토 C4(ADR-0065)가 이미 고친 것을 시험으로 고정했다.
- 새 ADR 19개(0064~0082), 새 마이그레이션 1개(0025, 보고서 버전의 데이터 기간), 새 설정 6개다.
  새 설정은 `MAX_REQUEST_BODY_BYTES`·`REPORT_PDF_MAX_CONCURRENT`·`CLIP_TIMEOUT_FACTOR`·`CLIP_TIMEOUT_MIN_S`·`WORKER_JOB_TYPES`·`WORKER_EXCLUDE_JOB_TYPES`이고,
  모두 `config.py`·`.env.example`·Compose에 넣었다.
- 계약이 바뀐 항목이 있어 웹 `api.gen.ts`와 에이전트 `client.gen.go`를 다시 만들었다.
- **아직 아무것도 `main`에 병합하지 않았다.** 풀 요청 세 개가 차례로 쌓여 병합을 기다린다.
- 사람이 정할 것은 22가지다(오늘 밤 새로 14, 계약 점검 3, 검토 2026-11-12에서 넘어온 5). 전부터 열린 HANDOFF 5장 결정은 따로 적었다.

## PR과 병합 순서

이 문서를 쓸 때는 PR 번호가 아직 없다. 브랜치 이름으로 찾는다.
세 PR 모두 기준(base)은 `main`이다. 그래서 앞 PR이 병합되기 전에는 뒤 PR에 앞 PR의 커밋도 함께 보인다.

| 순서 | 브랜치 | 담은 항목 | ADR |
|---|---|---|---|
| 1 | `fix/review-2026-11-12-rest` | 검토 2026-11-12의 W4·W1·V5·C4·C2·V9·V7·V8·C7(항목 8개). 루트 `CLAUDE.md`(`@AGENTS.md` 한 줄) | 0064~0071 |
| 2 | `fix/contract-sweep-2026-11-13` | 계약 점검 S01~S29·S39~S42(항목 7개: dsa·dsb·shr·deid·asa·asb·rec) | 0072~0078 |
| 3 | `fix/contract-sweep-2026-11-13-2` | 계약 점검 S30~S38·S43~S47(항목 4개: rpa·rpb·ctr·upl), 계약 생성물 재생성, 이 문서 | 0079~0082 |

병합하는 법:

1. 1번부터 아래에서 위로 하나씩 병합한다. 각 PR의 CI(`ci`·`web-e2e`)가 초록인 것을 본 뒤에 병합한다.
2. squash나 rebase가 아니라 **병합 커밋**으로 넣는다. 뒤 PR이 앞 PR의 커밋을 그대로 담고 있기 때문이다.
3. 앞 PR을 병합하면 뒤 PR에는 자기 커밋만 남는다. 그 상태의 CI를 보고 다음 PR을 병합한다.
4. 3번 PR에는 마이그레이션 0025(up·down 있음)가 있다. 운영에 배포할 때 이 마이그레이션이 적용돼야 한다.

## 항목별 요약

새 시험 파일 경로는 `backend/tests/` 아래다. '(추가)'는 기존 파일에 시험을 더한 것이다.

| 항목 | 무엇을 고쳤나 (한 줄) | ADR | 새 시험 파일 |
|---|---|---|---|
| W4 | 웹훅 수신 서버의 1xx 범람을 128KiB에서 끊어, 기한 내내 디스패처를 붙잡지 않고 보통 실패로 끝낸다 | 0064 | `unit/test_webhook_send.py`(추가), `integration/test_webhooks.py`(추가) |
| W1 | 웹훅 전송의 라운드 장벽을 없앴다. 전송마다 따로 보내고, 기관 몫에 보내는 중인 건도 센다 | 0070 | `integration/test_webhook_dispatch.py`, `integration/test_webhook_cross_tenant_isolation.py`(strict xfail 지움) |
| V5 | 앱에도 요청 본문 한도(기본 20MiB)를 두어, 넘으면 본문을 다 읽기 전에 413으로 답한다 | 0068 | `unit/test_request_body_limit.py`, `integration/test_request_body_limit_api.py` |
| C4 | 업로드 취소·만료 정리가 저장소 오류를 삼키지 않는다. 실패하면 세션·예약을 남기고 503이나 다음 주기에 다시 한다 | 0065 | `integration/test_upload_cleanup_storage_errors.py` |
| C2 | `RECEIVED`에 멈춘 자산을 운영자 명령 `requeue-verify`로 검증에 다시 넣는다 | 0071 | `integration/test_verify_requeue.py` |
| V9 | 계약 검증기의 문제 목록에서 위치 오류가 다른 결함을 가리지 않고, 쓸데없는 루트 오류와 시각 힌트를 바로잡았다 | 0067 | `unit/test_contract_problem_list_accuracy.py`, `integration/test_contract_validator_api.py`(추가) |
| V7·V8 | HWPX 양식을 올릴 때 끝까지 읽어 검사하고, PDF의 외부 주소 가져오기를 막고 동시 PDF 렌더 수를 묶었다 | 0066 | `unit/test_report_export_hardening.py`, `integration/test_report_export_hardening.py` |
| C7 | 클립 ffmpeg 한도를 구간 길이에 맞추고, 넘으면 재시도 없이 실패로 끝내며, 클립 전용 워커를 고를 수 있게 했다 | 0069 | `integration/test_clip_job_bounds.py` |
| dsa (S01·S03·S04·S05) | 큰 데이터셋 버전도 데이터 카드를 검사하고, 데이터셋 목록을 쪽으로 나누고, 계보 409와 항목 ID 400을 맞췄다 | 0072 | `integration/test_dataset_contract_checks.py`, `integration/test_dataset_build.py`(추가) |
| dsb (S02·S06·S07·S09·S10) | 다른 기관 이용 권한·신청에 404와 교차 기관 감사를 남기고, 없는 날짜와 기한 지난 승인을 거부한다 | 0073 | `integration/test_grant_access_request_tenant_audit.py`, `integration/test_access_request_period_dates.py`, `integration/test_access_decision_below_l3.py`, `integration/test_access_decision_deadline.py` |
| shr (S11·S12·S14·S15·S16) | 분류 변경 뒤 공유 등급을 자동으로 내리고 권한을 철회하며, 재신청·403 감사·409·빈 목록을 바로잡았다 | 0074 | `unit/test_scope_after_change.py`, `integration/test_classification_lowers_scope.py`, `integration/test_classification_family_locks.py`, `integration/test_share_resubmit_rules.py`, `integration/test_share_review_denial_audit.py`, `integration/test_share_request_conflict_contract.py`, `integration/test_share_candidates_body.py` |
| deid (S17·S19·S20·S21) | 원본 프레임 거부를 감사하고, 운영자는 심사 중일 때만 비식별본 프레임을 보며, 재검수 중복과 사람 태그를 바로잡았다 | 0075 | `integration/test_deid_frame_scope.py`, `integration/test_deid_recheck.py`, `integration/test_tag_human_edit.py` |
| asa (S22~S26) | 검증에서 실패한 자산의 재처리가 검증부터 다시 하고, data_role 변경의 감사 세 가지를 맞췄다 | 0076 | `integration/test_asset_patch_audit.py`, `integration/test_verify_resume.py`(추가) |
| asb (S27~S29) | 삭제 증명·다운로드 URL 거부를 감사하고, 클립 작업 목록에 `limit`·`cursor`를 더했다 | 0077 | `integration/test_deletion_certificate_cross_org_audit.py`, `integration/test_download_url_denial_audit.py`, `integration/test_clip_list_pagination.py` |
| rec (S39~S42) | 회차 기록의 null·없는 시각을 500 대신 400으로 답하고, 다른 기관 근거를 감사한다 | 0078 | `integration/test_record_contract_errors.py` |
| rpa (S30~S33) | 기간 보고서가 그 사업의 관측값만 세고, 9999년 끝 기간과 양식 버전 0을 400으로 답한다 | 0079 | `integration/test_period_report_scope.py`, `integration/test_report_template_hwpx_put.py` |
| rpb (S34~S38) | 보고서 버전마다 데이터 기간을 저장하고, 표시 없는 초안 HWPX를 막고, 내보내기 범위와 열람자 최신 버전을 맞췄다 | 0080 | `integration/test_report_contract_sweep.py` |
| ctr (S43·S44) | 사전 검증이 운영 경로와 같은 코드표·시각 순서 규칙도 본다 | 0081 | `unit/test_contract_prevalidate_rules.py`, `integration/test_contract_validator_rules.py` |
| upl (S45~S47) | 완료 호출의 저장소 장애를 409 대신 503으로 답하고, 파트 URL 한도를 계약에 적었다 | 0082 | `integration/test_upload_parts_complete_contract.py` |

## 사람이 정할 것

같은 질문은 하나로 합쳤다. '지금'은 세 PR을 병합한 뒤의 동작이다.

### 오늘 밤 새로 생긴 것

| # | 정할 것 | 지금 | 고를 길 | 기록 |
|---|---|---|---|---|
| 1 | 관측 항목 위치(`observations[].location`) 검사 (V9) | 스키마만 검사한다. 범위 밖·뒤바뀐 좌표는 배치 400이다. 관측값 위치는 저장하지 않는다 | (가) 그대로 둔다. (나) 위치 규칙을 적용한다. 항목별 거부면 계약 `observation_reject_reason`에 사유를 더하고 클라이언트를 다시 만든다. 배치 400도 가능하다. 위치를 저장할지도 함께 정한다 | ADR-0067, HANDOFF -1az |
| 2 | 정책 행이 없는 작업 유형 (C7) | `sensor_parse`·`tag_scenes`·`env_join`·`deidentify`·`collect_source`가 우선순위 0이라 클립(70)보다 뒤다. 0005의 `deid`·`tag` 행은 이름이 달라 쓰이지 않는다 | (가) 그대로 둔다. (나) 마이그레이션으로 행을 더하고 이름을 고친다. 동작이 바뀐다(예: `tag_scenes`가 GPU 없는 배치에서 기다린다) | ADR-0069 '남은 것', HANDOFF -1bb |
| 3 | 운영자에게 보이는 데이터셋 카드 (S01) | 가장 최근 버전(BUILDING·FAILED 포함)의 카드를 보여 준다 | (가) 그대로 둔다. (나) 최신 READY 버전의 카드로 바꾼다 | ADR-0072 결정 5 |
| 4 | 분류 변경 뒤 공유 등급 하향 (S11) | 조건을 못 맞추면 자동으로 내리고 권한은 늘 즉시 철회한다. L4→L3처럼 공유가 이어져도 그 자산이 든 버전의 권한이 모두 끊긴다. 분류가 정하는 조건만 바로잡는다 | (가) 그대로 둔다. (나) 하향 대신 422로 거부한다. (다) 권한 처리(즉시 철회 / 만료까지 유지)를 고르게 한다 | ADR-0074 감수할 점 |
| 5 | 하향 감사 모양 (S11·S25) | CORE 변경 하향은 자산마다 새 감사 동작 `SHARE_SCOPE_DOWNGRADED`를 쓴다. 분류 변경 하향은 `ASSET_CLASSIFIED` details와 `GRANT_REVOKED`(cause `ASSET_CLASSIFIED`)를 쓴다 | 한 모양으로 맞출지 정한다. 분류 변경에도 `SHARE_SCOPE_DOWNGRADED`를 쓰면 내린 파생본을 target_id로 찾을 수 있다 | ADR-0074, ADR-0076 |
| 6 | 승인 뒤(APPROVED)를 '심사 중'으로 볼지 (S19) | 보지 않는다. 승인된 원본의 비식별본에 재검수가 열려도 운영자는 프레임을 못 본다(배정 검수자는 본다) | (가) 그대로 둔다. (나) `backend/app/api/deid.py`의 `UNDER_REVIEW`에 APPROVED를 더한다(한 줄) | ADR-0075 |
| 7 | 사람이 지운 날씨 태그 (S21 이웃) | 사람이 REMOVE한 날씨 태그를 뒤의 날씨 보정이 다시 달 수 있다 | 막을지 정한다 | ADR-0075 결과 |
| 8 | 교차 기관 사례를 수용 표에 넣을지 (S02·S06·S27) | 이용 권한·신청의 교차 기관 사례는 S3가 필요해 통합 시험 파일에만 있다. 삭제 증명 사례도 T01 `CROSS_ORG_CASES`에 없다 | T01 수용 표에 넣을지 정한다 | 이 문서(dsb·asb 항목 메모) |
| 9 | 응답 기한이 지난 이용 신청 (S10) | 승인은 409로 거부하고 거절은 받는다. `DENIED`로 바꾸는 것은 정비 주기(기본 60초)를 기다린다 | (가) 그대로 둔다. (나) 요청 안에서 바로 `DENIED`로 바꾼다 | ADR-0073 |
| 10 | 이미 저장된 기간 보고서 (S30) | 저장된 버전(승인·제출본 포함)에는 다른 사업 값이 섞인 채 남는다. 장비의 사업과 회차 장비 목록은 지금 값으로 본다 | 제출된 기간 보고서를 새 버전으로 다시 만들지 정한다. 정확한 사업 이력이 필요하면 관측값에 `project_id` 열을 더한다(파티션 표 마이그레이션) | ADR-0079 |
| 11 | 초안 HWPX의 표시 범위 (S35) | 초안은 `{{report.status}}` 자리만 요구한다. 생성 시각·데이터 기준 시각 자리는 강제하지 않고, SUPERSEDED는 막지 않는다 | 실제 관리기관 양식(T11-f)을 본 뒤, 요구사항 4.4·ADR-0018대로 더 막을지 정한다 | ADR-0080 |
| 12 | 공개 예시와 시드 (S43) | `contracts/examples/event.manual-safety.json`(`SAFETY_INCIDENT.near_miss`)이 시드 기관의 사전 검증에서 무효다. 운영 `POST /v1/events`는 전부터 422였다 | (가) 예시를 공통 상위분류로 바꾼다. (나) 시드에 하위코드를 등록한다 | ADR-0081 |
| 13 | 파트 URL 한도 설정 (S47) | 한도(기본 1000)는 `config.py`의 `max_part_urls_per_response`에만 있어 운영에서 바꿀 수 없다 | `MAX_PART_URLS_PER_RESPONSE`를 `.env.example`·Compose에 넣을지 정한다 | ADR-0082 |
| 14 | 근거의 다른 기관 ID 감사 행 수 (S41) | 대상마다 한 행을 남긴다. 요청 하나가 최대 500행을 쓸 수 있다 | 행을 묶을지 정한다. HANDOFF 5장 '인증 실패 감사 집계'(ADR-0049)와 같은 질문이라 함께 정한다 | ADR-0078 |

### 계약 점검에서 고치지 않은 것 (S08·S13·S18)

| # | 정할 것 | 지금 | 고를 길 | 기록 |
|---|---|---|---|---|
| 15 | 이용 신청의 `agreement_version` (S08) | 계약은 필수인데 구현은 빠져도, null이어도 201이다. 웹 폼은 보내지 않는다. 저장된 신청 어디에도 약정 판이 없다 | (가) 약정 판을 필수 증거로 본다. 구현에서 필수로 하고 웹이 지금 판을 보낸다. 판 값을 어디에 둘지도 정한다. (나) 계약 required에서 빼거나 선택이라고 적는다 | `contract-sweep-findings.md` S08 |
| 16 | 검수자의 공유 심사 접근 (S13) | 검수자는 공유 심사·신청 조회와 처리에서 늘 404와 `CROSS_ORG_ACCESS_ATTEMPT` 감사를 받는다. 검수자의 QUALITY_REVIEW→REPROCESS는 비식별 검수 결정으로 된다. 검증 의견이 갈렸다 | (가) `permissions.py`의 `share:read`·`share:review`에서 REVIEWER를 빼고 계약 요약을 고친다. (나) 검수자에게 배정 기반 자산 범위를 준다(`tenant._role_filter`, 모든 자산 경로가 넓어져 ADR 필요) | `contract-sweep-findings.md` S13, HANDOFF -1be |
| 17 | 원본이 심사 중이 아닐 때의 반려 (S18) | 승인된 원본의 재검수나 공유 철회·반려 뒤에 REJECT(수동 영역 MISSED 포함)를 보내면 200인데 재처리가 없다. 작업은 SKIPPED로 끝나고 비식별본은 FAILED로 남는다 | 승인된 원본의 재검수 반려가 무엇을 뜻할지(domain.md 5장에 APPROVED→REPROCESS가 없다), 철회·반려가 열린 검수를 취소할지 정한다. 발견 문서의 임시안은 원본이 QUALITY_REVIEW·REPROCESS가 아니면 409로 거부하고 계약에 적는 것이다 | `contract-sweep-findings.md` S18 |

### 검토 2026-11-12에서 넘어온 것 — 오늘 밤 뒤 상태

| # | 정할 것 | 오늘 밤 뒤 상태 | 고를 길 | 기록 |
|---|---|---|---|---|
| 18 | 삭제를 2단계(outbox)로 할지 (C1) | 바뀌지 않았다. 객체 삭제를 확인한 뒤에야 증명을 발급한다(실패하면 503) | (가) 그대로 둔다. (나) 2단계로 하고 증명을 '삭제 요청됨'·'삭제 확인됨'으로 나눈다(증명의 법적 의미가 바뀐다) | ADR-0055, 검토 README '사람이 정할 것' 1 |
| 19 | `RECEIVED`에서 실패로 가는 길 (C2) | 좁혀졌다. 운영자 명령 `python -m app.cli requeue-verify`로 검증 작업을 다시 넣을 수 있다(-1ay). 검사기가 계속 없으면 예약은 묶인 채다 | `RECEIVED → FAILED(예약 해제)` 전이를 domain.md 5장에 더할지, 'RECEIVED 장기 체류' 감시 규칙 코드를 둘지 정한다 | ADR-0058·0071, 검토 README '사람이 정할 것' 2 |
| 20 | 웹훅 구독 비활성화의 의미 (W5) | 좁혀졌다. 라운드 장벽(W1)은 고쳤다(-1av). 독살 이벤트 하나가 건강한 구독을 끄고 다른 대기 이벤트를 취소하는 것은 그대로다 | 연속 실패 N회 같은 규칙으로 바꿀지 정한다(ADR-0030·0041 개정) | ADR-0070, 검토 README '사람이 정할 것' 3 |
| 21 | 미완성 멀티파트 청소 (C4) | 좁혀졌다. 정리가 실패하면 세션·예약을 남겨 다시 한다(-1ax) | 저장소 수명주기 규칙(`AbortIncompleteMultipartUpload`)을 쓸지(SeaweedFS 지원은 확인 필요), 수정 전에 남은 것을 한 번 대조 점검할지 정한다 | ADR-0065, 검토 README '사람이 정할 것' 4 |
| 22 | 요청 본문 크기 한도 (V5) | 임시로 정했다. 프록시 한도는 그대로 두고 앱에도 같은 20MiB를 두었다(-1aw) | (가) 그대로 둔다. (나) 앱 한도를 프록시보다 줄인다. (다) 앱 한도를 뺀다(되돌리는 법은 ADR) | ADR-0068, 검토 README '사람이 정할 것' 5 |

### 전부터 열린 것 (HANDOFF 5장)

- **운영자 기관 자산의 긴급 열람**: 열려 있다. 오늘 밤 S19의 비식별본 프레임 검사도 원본과 같이 운영자 기관(ORG_OPERATOR)이 가진 자산에는 적용하지 않는다(ADR-0075). 이 결정을 따른다.
- **인증 실패 감사 집계**: 열려 있다. 위 14번(S41)이 같은 질문이다.
- **실증지표·보고서 양식**: 열려 있다. 실제 HWPX 양식은 아래 '운영에서 확인할 것' 6번의 조건을 맞춰야 한다.
- 그 밖의 행(C0 세션 역전이, 체크섬 불일치 격리 재수집, LineageEdge.relation, PSF-2.0 예외, Keycloak MFA, 구성원 초대, 공공 Open API, 비식별·태깅 모델, 보고서 PDF 라이선스, 감시 임계값, 운영 메신저, 운영자의 원 관측값·보고서 초안 열람, 운영자의 원래 파일명 열람, 장비 등록 코드, 등록 한도 키, 프록시 전용 망 웹훅, 기존 결정 대기)은 오늘 밤 손대지 않았다. 그대로 열려 있다.

## 고치지 않은 새 발견

오늘 밤 찾았지만 고치지 않았고, 사람 결정도 아닌 것이다. 다음 묶음의 후보다.

| 무엇 | 드러난 곳 | 기록 |
|---|---|---|
| 회차 시각이 9999-12-31T15:00:00Z 이후인 회차의 회차 보고서가 500이다. 회차 등록(`POST /v1/sessions`)이 그 시각을 받는다 | rpa | ADR-0079 감수할 점, HANDOFF -1bj, 발견 문서 S31 |
| 스토리지 읽기 실패로 재시도를 다 쓴 자산은 FAILED(VALIDATING에서)가 되고 업로드·쿼터 예약이 풀리지 않는다. 영상·센서는 이제 재처리로 검증부터 다시 하지만(S22), 그 밖의 유형은 재처리가 409라 되돌릴 길이 없다 | rq, asa | ADR-0071·0076 |
| 공유 심사 WITHDRAW와 공유 등급 신청의 하향에는 권한마다 `GRANT_REVOKED`가 없다 | asa | ADR-0076, 발견 문서 S25 |
| 회차 지정이 실패해 되돌아간 CORE 변경에서도 이용 기관 철회 알림 메일은 이미 나갔을 수 있다(전부터) | asa | ADR-0076 |
| `request_clip`의 같은 구간 작업 찾기가 최근 100건까지만 본다. 더 오래된 QUEUED·RUNNING 작업이 있으면 같은 작업이 또 들어갈 수 있다 | asb | ADR-0077 |
| 다른 경로의 감사 없는 403(`observations.py`·`datasets.py`·`reports.py`·`assets.py`의 몇 곳, 줄 번호는 shr 브랜치 기준). 다운로드 URL과 원본 프레임은 S28·S17에서 고쳤다 | shr | ADR-0074 |
| `createSessionRecord`·`createAccessRequest`는 `Idempotency-Key`를 받지만 409를 계약에 적지 않았다. `asset_ids: null`은 아직 받는다 | shr | ADR-0074 |
| `api/sharing.py`·`api/datasets.py`의 성공 감사 몇 가지가 요청 트랜잭션과 따로 확정된다. ADR-0053과 다를 수 있다(확인 필요) | shr, dsb | ADR-0073·0074 |
| 처리기 없는 `parse_rfc3339` 호출이 다른 라우터에도 있다(`external.py`·`agents.py`·`catalog.py`·`monitoring.py`). 같은 500이 나는지는 확인하지 않았다 | rec | ADR-0078 |
| `getUpload`가 파트 목록 저장소 오류를 빈 목록으로 삼킨다 | upl | ADR-0082 |
| 크기 불일치 취소에서 멀티파트 중단 응답이 유실되면, 다시 보낸 완료나 재개 조회가 만료 때까지 막힌다(전부터) | c4 | ADR-0065 감수할 점 |
| 에이전트는 완료가 5xx면 `GET .../parts`로 재개해, 저장소에서 끝난 완료를 마저 끝내는 재시도 경로에 닿지 않을 수 있다 | c4 | ADR-0065 |
| 천천히 흘리는 웹훅 수신 서버는 여전히 기한까지 간다(수정 전과 같다). 막으려면 읽기 횟수나 속도 하한을 따로 정해야 한다. TLS 안의 비응용 레코드는 세지 않는다(확인 필요) | w4 | ADR-0064 |
| 기본 워커 하나 배치에서는 900초 이상 구간 클립이 최대 `FFMPEG_TIMEOUT_S` 동안 워커를 쥔다. 전역 클립 동시 한도, 다른 긴 작업의 임대 갱신, 원본 받기·올리기 시간 한도, `worker-clips` 지표 수집이 없다 | c7 | ADR-0069 |
| PDF 렌더는 아직 요청 안에서 10초대가 걸린다(워커 이전·시간 상한 미완). 같은 이름 zip 항목, 큰 XML 항목의 내보내기 메모리, RLIMIT_AS 아래 lzma 큰 사전의 500이 남았다 | rep | ADR-0066 |
| 웹 화면이 새 응답을 따로 안내하지 않는다: 보고서 내보내기의 429·409, 기한 지난 승인의 409. 클립 목록에 더 보기 버튼이 없고, 데이터셋이 500개를 넘으면 웹도 쪽 넘김이 필요하다 | rep, rpb, dsb, asb, dsa | 각 ADR |
| `Content-Length`가 약 4300자리를 넘으면 `int()`가 실패해 500이 될 수 있다. uvicorn이 먼저 거절할 것으로 보지만 실행하지 않았다(확인 필요) | v5 | v5 항목 메모 |

## 운영에서 확인할 것

항목들이 운영 데이터와 운영 환경에서 확인해 달라고 남긴 것이다.

1. **멈춘 자산**: `requeue-verify --all-stuck --dry-run`으로 `RECEIVED`에 멈춘 자산을 찾는다(Runbook 11). 수정 전에 재처리로 VALIDATING에 갇혔거나, 겹친 재처리로 등록 뒤 FAILED가 된 자산도 찾는다. 이들은 자동으로 풀리지 않는다(C2, S22).
2. **미완성 멀티파트와 고아 객체**: 수정 전에 끝난 세션이 저장소에 남긴 것은 이번 변경으로 정리되지 않는다. 대조 점검을 할지는 위 21번이다(C4).
3. **틀린 데이터 카드**: 수정 전에 큰 버전 경로로 받은 틀린 카드가 운영 DB에 있는지 본다. 있으면 그 데이터셋의 운영자 조회가 계약을 어긴다. 새 버전으로 다시 만들면 풀린다(S01).
4. **제출된 기간 보고서**: 다른 사업 값이 섞인 기간 보고서가 제출돼 있는지 본다. 다시 만들지는 위 10번이다(S30).
5. **보고서 버전 기간**: 마이그레이션 0025는 기존 버전을 지금 보고서 행의 기간으로 채운다. 그 전에 새 버전 때문에 기간이 바뀐 승인·제출본은 바뀐 값으로 남는다. 원래 값은 EVIDENCE 존 승인 스냅숏에 있고, 되살리는 도구는 없다(S34).
6. **실제 HWPX 양식(T11-f)**: UTF-8인지, 일반 항목 이름만 쓰는지(`:`와 서식 문자 이름은 거부한다), `{{report.status}}` 자리가 있는지 본다. 자리가 없으면 초안 내보내기가 409다(V7, S35).
7. **클립 한도 배수 4**: 개발 PC의 합성 영상으로 정한 값이다. 운영 하드웨어와 실제 해상 영상으로 다시 잰다(C7).
8. **SeaweedFS 동작**: 수명주기 규칙 지원, 완료 거부에 쓰는 오류 코드, 최소 파트 크기 강제는 확인 필요다(C4, S46).
9. **PDF 동시 렌더 기본값**: `REPORT_PDF_MAX_CONCURRENT=2`와 `Retry-After` 5초는 실측으로 고른 값이 아니다. 렌더러 라이브러리를 실행 중에 설치하면 프로세스를 다시 띄워야 PDF가 켜진다(V8, S37).

## 밤 작업 방식

항목마다 한 사람(에이전트)이 회귀 시험을 먼저 쓰고 구현했다. 검토자 셋이 따로 검토했고, 기준 코드에서 새 시험이 실패하는지 다시 확인했다. 지적을 고친 뒤 마지막 검증을 거쳤다. 그다음 항목 브랜치를 세 묶음 브랜치에 차례로 cherry-pick하고, 항목마다 문서 커밋과 빠른 검사를 했다. 마지막 묶음의 끝에서 백엔드 전체 시험, 정적 검사, 웹·Go 검사로 전체를 로컬에서 증명한다. 그 결과와 sha는 각 PR 본문에 적는다.
