# 2026-11-20 통합 검토 기록 — 밤 작업 묶음 A·B·C를 합친 줄기

2026-11-12·13 밤 작업을 세 묶음으로 쌓은 줄기(`fix/review-2026-11-12-rest` → `fix/contract-sweep-2026-11-13` →
`fix/contract-sweep-2026-11-13-2`)를 합친 상태로 다시 검토했다. 이 문서는 **무엇을 지적받았고, 어떻게 확인했고,
무엇을 이 PR에서 고쳤고, 무엇이 사람 결정으로 남았는지**를 적는다. 진행 요약은 루트 `HANDOFF.md` 0장이다.

## 무엇을 봤나

- 대상: 묶음 A·B·C를 합친 줄기. 웹훅·클립·공유·자산은 그때 B의 끝(4e9b0ac)에서, 업로드·보고서·계약 검증은 그때 C의 끝(a46952e)에서 봤다.
  그 뒤 A·B·C는 `test_webhook_cross_tenant_isolation.py` 하나만 바꿔 다시 쌓았으므로 고친 커밋은 그대로 얹힌다.
- 영역마다 세 관점으로 따로 봤다.
  - 통합·정확성: 두 항목이 만나는 곳의 충돌 해결, 잠금 순서, 서로 다른 항목의 상호작용.
  - 보안·기관 격리·감사: 다른 기관 자원이 보이는지, 권한 거부·철회의 감사, 한 요청이 자원을 다 쓰는지.
  - 계약·데이터·운영: 계약 문구와 실제 응답, 문서·runbook·ADR 문장, 설정과 배포.
- 마지막으로 각 항목의 범위 메모(scope_notes)에 '고치지 않음'으로 적힌 것을 다시 분류했다(아래 '범위 메모 분류').

## 어떻게 확인했나

- 지적마다 **읽는 검증자**가 코드를 다시 읽고 판정했다.
- 심각도가 중간·높음이면 **돌려 보는 검증자**가 따로 합성 데이터로 재현했다.
- 판정: **확인**(두 검증자 모두 맞다) / **판정 갈림**(한쪽은 맞다, 한쪽은 범위 밖·이미 적힌 일이라고 봤다) / **반박**(지적이 틀렸거나,
  항목의 범위 메모·ADR에 이미 적힌 범위 밖 일이다).
- 확인된 것 가운데 고치기보다 사람이 정해야 하는 것은 '사람이 정할 것'으로 두었다.
- 지적 ID가 없던 것은 영역 이름에 O(사람 결정)·D(판정 갈림)·R(반박)과 번호를 붙였다.

| 판정 | 개수 |
|---|---|
| 확인 — 이 PR에서 고침 | 33 |
| 확인 — 사람이 정할 것 | 5 |
| 판정 갈림 | 3 |
| 반박 | 14 |
| 합계 | 55 |

심각도는 검증 뒤의 값이다. 검증자가 바꾼 것은 '처음 → 검증 뒤'로 적었다.

## 웹훅 전송

| id | 지적 | 심각도 | 판정 | 처리 |
|---|---|---|---|---|
| WEBHOOKS-1 | ADR-0064는 범람 시도의 지속 부하가 작다고 했지만, 백오프는 전송마다라 구독 하나의 시도 횟수를 묶지 않는다. W1 뒤에는 실패한 시도 직후 그 구독의 다음 대기 건을 바로 가져간다 | 낮음 | 확인 | 고침 (이 PR) |
| WEBHOOKS-2 | ADR-0064가 백오프로 1xx 범람의 지속 부하가 작다고 하지만, W1 뒤 쌓인 구독은 연달아 다시 시도된다 | 낮음 | 확인 | 고침 (이 PR) |
| WEBHOOKS-3 | W1 뒤 ADR-0064가 아직 라운드 구조를 말하고, 쌓인 구독의 지속 부하가 작다는 문장이 틀렸다 | 낮음 | 확인 | 고침 (이 PR) |
| WEBHOOKS-4 | `WEBHOOK_DISPATCH_INTERVAL_S` 설명이 쉴 때의 조회 간격만 말한다. ADR-0070부터는 보내는 중일 때 새로 기한이 된 건이 기다리는 시간도 정한다 | 낮음 | 확인 | 고침 (이 PR) |
| WEBHOOKS-5 | 04번 design.md 전송 규칙에 W4의 128KiB 응답 헤더 상한이 빠졌다 | 낮음 | 확인 | 고침 (이 PR) |
| WEBHOOKS-R1 | W1의 새 디스패처 지표가 클립 전용 워커(C7)의 두 번째 디스패처를 빠뜨린다(worker-clips는 수집 대상이 아님) | 낮음 | 반박 | C7 범위 메모가 worker-clips의 Prometheus 수집을 범위 밖으로 적었고 ADR-0069에도 있다. 워커 복제가 여럿일 때도 같은 배포 수집 문제다 |

- **WEBHOOKS-1 ADR-0064 지속 부하 문장** — **고침** (문서, `test_webhooks.py::test_interim_flood_backlog_is_attempted_back_to_back`). 백오프는 전송마다라 범람 구독에 쌓인 N건이 차례로 시도된다고 바로잡고, 구독이 꺼질 때(기본 약 64분)까지 부하가 기관의 이벤트 수에 비례한다고 적음. 구독 단위로 묶는 것은 W5(사람 결정)
- **WEBHOOKS-2 백오프 근거 오류** — **고침** (WEBHOOKS-1과 같은 문장). 라운드 디스패처 때도 같았고 W1이 만든 문제가 아님을 적고, 통합 검토 측정(메인 스레드 95~97%)과 ADR-0070이 버린 '구독 쉬게 하기'를 인용
- **WEBHOOKS-3 ADR-0064의 라운드 서술** — **고침**. ADR-0064에 갱신 절(ADR-0070)을 더해 결정 3·버린 대안의 '라운드'가 전송마다 따로·보내는 중인 건까지 세는 기관 몫으로 바뀌었음을 적음
- **WEBHOOKS-4 WEBHOOK_DISPATCH_INTERVAL_S 설명** — **고침**. `config.py`·`.env.example` 설명에 보내는 중인 건이 끝나지 않을 때 새로 기한이 된 건을 다시 보는 간격이기도 하다는 것(ADR-0070)을 더함
- **WEBHOOKS-5 design.md 응답 헤더 상한 누락** — **고침**. 04번 `design.md` 전송 ②에 `RESPONSE_HEAD_MAX_BYTES`(128KiB)·`RemoteProtocolError`·ADR-0064, ③에 백오프는 실패한 그 건만 미룬다는 것, 시험 목록에 1xx 범람 시험을 더함

## 클립

| id | 지적 | 심각도 | 판정 | 처리 |
|---|---|---|---|---|
| CLIPS-1 | 클립 전용 워커가 `make up` 뒤 다시 오르지 않아, 다시 시작한 뒤 클립이 QUEUED에 계속 남는다 | 중간 | 확인 | 고침 (이 PR) |
| CLIPS-2 | worker-clips도 자기 환경으로 정비 주기 전체를 도는데, runbook은 `worker`만 다시 시작하거나 다시 만든다 | 중간 | 확인 | 고침 (이 PR) |
| CLIPS-3 | `CLIP_TIMEOUT_FACTOR`가 inf(또는 곱이 넘치는 큰 값)를 받아, 그 뒤 모든 클립 작업이 OverflowError로 실패한다 | 낮음 | 확인 | 고침 (이 PR) |
| CLIPS-4 | `CLIP_TIMEOUT_FACTOR`가 inf·큰 값을 받아 클립 작업마다 OverflowError로 INTERNAL 재시도된다(CLIPS-3과 같은 지적) | 낮음 | 확인 | 고침 (이 PR) |
| CLIPS-5 | 긴 클립이 기본 워커를 최대 `FFMPEG_TIMEOUT_S`(1시간) 쥔다고 적었지만, 코드는 그 두 배와 전송 시간까지 허용한다 | 낮음 | 확인 | 고침 (이 PR) |

- **CLIPS-1 클립 전용 워커 프로필이 다시 시작 뒤 빠짐** — 켜는 법(runbook 07 5번·`.env.example`·Compose 주석·ADR-0069)이 이제 `COMPOSE_PROFILES=clip-worker`를 `WORKER_EXCLUDE_JOB_TYPES=clip_export`와 함께 `.env`에 두라고 적는다. 그래서 `make up`·`make down`(재부팅 뒤 기동 포함)에서 worker-clips도 함께 오르내린다. `make up`은 기본 워커가 clip_export를 가져가지 않는데 worker-clips가 꺼져 있으면 경고한다(`test_clip_worker_ops_docs.py`, `tools/tests/test_compose_up.py`).
- **CLIPS-2 runbook이 worker만 다시 시작함** — runbook 03 5번·06 2·5번·07 1·2·5번이 이제 worker-clips도 함께 다시 만들거나 다시 시작한다. 11 2번에는 다시 만들 필요가 없다고 적었다. ADR-0069 감수할 점에 두 워커가 각자 환경으로 정비 주기를 돈다는 것을 적었다. 전용 워커가 주기 작업을 건너뛰는 설정은 두지 않았다(까닭은 ADR)(`test_clip_worker_ops_docs.py`).
- **CLIPS-3 CLIP_TIMEOUT_FACTOR inf로 클립마다 OverflowError** — 이제 inf·nan이면 `ConfigError(CLIP_TIMEOUT_FACTOR)`로 기동하지 않는다. 곱이 넘치는 큰 배수는 `FFMPEG_TIMEOUT_S`로 자른다(`test_clip_timeout_factor.py`).
- **CLIPS-4 CLIP_TIMEOUT_FACTOR inf·큰 값 (CLIPS-3과 같은 지적)** — CLIPS-3과 같은 수정으로 고쳤다.
- **CLIPS-5 긴 구간의 최악 점유 시간을 낮게 적음** — ADR-0069에 인코딩 + ffprobe 다시 읽기 = 한도의 두 배(기본 2시간) + 원본 받기·올리기라고 적었다. 코드는 그대로다. HANDOFF -1bb와 `docs/review-2026-11-12/README.md` C7 행의 '최대 한 시간'도 이 PR의 문서 커밋에서 같은 문장으로 고쳤다.

## 공유

| id | 지적 | 심각도 | 판정 | 처리 |
|---|---|---|---|---|
| SHARING-1 | 원본 분류 지정이 같은 회차의 미탐 검수 결정과 교착(409)할 수 있다(shr `lock_family` × deid S20 회차 재검수) | 낮음 | 확인 | 고침 (이 PR) |
| SHARING-O1 | 분류 지정으로 공유 등급이 낮아질 때 운영자의 내부 분류 사유가 이용기관에 전달된다 | 중간 → 낮음 | 확인 | 사람이 정할 것 |
| SHARING-D1 | 기관 관리자의 공유 등급 신청 하향이나 공유 심사 WITHDRAW로 철회된 이용 권한에 GRANT_REVOKED 감사가 없다(분류 지정·data_role 경로와 다름) | 중간 | 판정 갈림 | 판정 갈림 — 사람이 정할 것 |
| SHARING-2 | 제공기관 ORG_ADMIN이 공유 데이터셋의 다른 기관 버전·데이터 카드·샘플 썸네일 URL을 본다 | 낮음 | 확인 | 고침 (이 PR) |
| SHARING-3 | 이용 신청·이용 권한·이용 이력 목록에 쪽 나눔과 상한이 없다 | 낮음 | 확인 | 고침 (이 PR) |
| SHARING-O2 | 운영자 분류 지정으로 끊긴 이용 권한이 운영자의 내부 사유를 '제공기관 공유 등급 하향' 머리말로 이용기관에 보여 준다 | 낮음 | 확인 | 사람이 정할 것 |
| SHARING-4 | markShareCandidates가 형식이 틀린 asset_id에 아직 400 대신 404를 준다(S16은 빈 목록·중복만 고침) | 낮음 | 확인 | 고침 (이 PR) |
| SHARING-5 | 같은 uniqueItems 위반이 S05와 S16에서 다른 오류 포인터를 받는다 | 낮음 | 확인 | 고침 (이 PR) |
| SHARING-O3 | 요청 본문 스키마를 어긴 data_card가 400 VALIDATION_ERROR 대신 422 RULE_VIOLATION을 받는다(S01이 큐 경로로도 넓힘) | 낮음 | 확인 | 사람이 정할 것 |

- **SHARING-1 같은 회차 재검수와 분류 지정의 교착** — 미탐 결정이 같은 회차의 다른 원본 비식별본에 재검수를 열 때, 그 원본을 먼저 잠근 뒤 비식별본을 잠근다(`deid._recheck_session`, 원본 ID 순서). 이제 분류 지정(`lock_family`)과 순서가 같아, 둘이 겹쳐도 409나 결정 실패 없이 차례로 끝난다. ADR-0074 결정 4와 `lock_family` 설명도 바로잡았다(ADR-0088). 여러 계보를 엇갈려 잠그는 드문 경우(같은 회차 미탐 결정끼리, 일괄 삭제)는 ADR-0053대로 409로 남긴다.
- **SHARING-2 제공기관에게 보이던 다른 기관 버전** — 제공기관 주체는 `GET /v1/datasets`·`GET /v1/datasets/{id}`에서 자기 기관 자산이 든 버전만 본다. 데이터 카드와 샘플 썸네일 URL도 그 버전에서만 고른다. 계보 조회와 같은 규칙이고, 계약 설명에 적었다(ADR-0088).
- **SHARING-3 쪽을 나누지 않던 이용 목록** — `GET /v1/access-requests`·`/v1/grants`는 ID 순으로, `/v1/usage-history`는 (초 단위 at, kind, 사건 ID) 순으로 `limit`·`cursor`대로 나누고 `next_cursor`를 준다. 이용 이력은 신청마다 따로 읽지 않고 질의 하나로 모은다. 계약을 먼저 고쳤고, 웹은 `apiAll`로 끝까지 따라 읽는다(ADR-0088).
- **SHARING-4 공유 후보의 형식이 틀린 ID** — `asset_ids` 항목과 `session_id`가 계약 형식이 아니면 404 대신 400 `VALIDATION_ERROR`(`/asset_ids/<i>`·`/session_id`)다. 이때 아무 자산도 후보가 되지 않는다. 정규식은 `domain.ids.id_pattern` 한 곳에서 만든다(ADR-0088).
- **SHARING-5 중복 ID 오류의 두 모양** — 공유 후보와 데이터셋 버전 모두 배열 자리 하나(`/asset_ids`·`/item_asset_ids`, `code: unique`)로 답한다. 다시 나온 ID는 다섯 개까지 메시지에 적는다(`errors.duplicate_items`). 포인터 자리는 웹훅·재처리·JSON Schema 검사와 같다(code 값은 JSON Schema 검사의 `schema.uniqueItems`·정책 초안의 `uniqueItems`와 아직 다르다). ADR-0072 결정 4의 틀린 문장도 바로잡았다(ADR-0088).

## 자산·비식별

| id | 지적 | 심각도 | 판정 | 처리 |
|---|---|---|---|---|
| ASSETS-1 | PATCH data_role CORE가 파생본 행을 원 자산보다 먼저 잠가(ADR-0074 순서의 반대) 분류 지정·결정·삭제와 교착하고 한쪽이 409를 받는다 | 낮음 | 확인 | 고침 (이 PR) |
| ASSETS-R1 | 병렬 항목 둘이 같은 공유 등급 하향을 다르게 감사한다(CORE 변경은 SHARE_SCOPE_DOWNGRADED, 분류 지정은 아님) | 낮음 | 반박 | shr 범위 메모와 ADR-0074 감수할 점에 두 모양이 이미 적혀 있고, 하나로 맞출지는 거기서 정할 일로 남겼다 |
| ASSETS-2 | 운영자 긴급 열람 거부의 새 PERMISSION_DENIED 감사 두 곳이 다른 사유 코드를 쓴다(no_break_glass_grant / no_active_break_glass) | 낮음 | 확인 | 고침 (이 PR) |
| ASSETS-3 | data_role CORE 변경이 파생 자산을 잠그지 않아, 동시에 확정된 상향이 CORE 원본의 파생본을 L2 이상·이용 권한 유지로 남긴다 | 중간 → 낮음 | 확인 | 고침 (이 PR) |
| ASSETS-4 | 재처리가 자산을 잠그고 다시 읽지만 deleted_at을 다시 보지 않아, 그사이 삭제된 자산이 202·처리 작업·SUCCESS 감사를 받는다 | 낮음 | 확인 | 고침 (이 PR) |
| ASSETS-R2 | 클립 목록의 쪽마다 자산의 CLIP 계보 행(출처 JSON 포함)을 모두 읽는다 | 낮음 | 반박 | ADR-0077 결정 5가 일부러 정한 것이다. 체크섬 중복 제거 때문에 쪽의 작업 ID로 좁히면 결과 자산을 놓친다 |
| ASSETS-5 | GET /v1/deid-reviews에 쪽 한도가 없어, 운영자에게 모든 검수를 행마다 자산 조회와 함께 준다 | 낮음 | 확인 | 고침 (이 PR) |
| ASSETS-O1 | 같은 회차 재검수가 L2 이상으로 공유된 비식별본의 review_result를 지우고 공유 등급·이용 권한은 그대로 둬, GET /v1/assets/{id}가 계약을 어긴다 | 중간 | 확인 | 사람이 정할 것 |
| ASSETS-6 | Runbook 11(과 C2 HANDOFF 항목·ADR-0071)이 스토리지 장애로 실패한 자산(failed_from=VALIDATING)은 복구할 수 없다고 하지만, S22부터 재처리로 복구된다 | 낮음 | 확인 | 고침 (이 PR) |
| ASSETS-7 | 같은 '유효한 긴급 열람 승인 없음' 거부를 오늘 밤 더한 두 항목이 다른 사유 코드로 감사한다(ASSETS-2와 같은 지적) | 낮음 | 확인 | 고침 (이 PR) |

- **ASSETS-1 CORE 변경의 잠금 순서** — 고침(ADR-0076 보완, `test_core_change_family_locks.py`). data_role CORE 변경은 판단 전에 `sharing.lock_family`로 원 자산 → 파생본 순으로 잠그고 다시 읽는다. 분류 지정·삭제·검수 결정과 같은 순서가 되어, 파생본을 쥔 채 원 자산을 기다리다 교착해 한쪽이 409를 받던 일이 없다.
- **ASSETS-2 긴급 열람 거부 사유 코드** — 고침(ADR-0075 결정 1 수정, `test_deid_frame_scope.py`). 검수 원본 프레임 거부 감사도 다운로드 URL(S28)과 같은 `reason: no_active_break_glass`를 쓴다. action은 그대로 다르다(`deid_review:read`·`asset:download`).
- **ASSETS-3 CORE 변경과 겹친 상향** — 고침(ASSETS-1과 같은 잠금, `test_core_change_family_locks.py`). 겹친 L1→L2 상향 신청은 CORE 변경의 확정을 기다린 뒤 CORE 원본의 파생본이라 422가 되고, 파생본은 L1에 남는다(전: CORE 원본 + L2 파생본).
- **ASSETS-4 재처리와 겹친 삭제** — 고침(ADR-0076 보완, `test_reprocess_deleted_asset.py`). 재처리가 잠그고 다시 읽은 자산이 그사이 삭제됐으면 404이고, 처리 작업·REPROCESS_REQUESTED 감사·재처리 시각을 남기지 않는다.
- **ASSETS-5 비식별 검수 목록 페이지** — 고침(ADR-0089, `test_deid_review_list_pages.py`, 계약 변경). listDeidReviews에 `cursor`·`limit`(기본 50·최대 500)·필수 `next_cursor`·400을 더했다. 만든 시각·검수 ID 키셋으로 넘기고, 커서는 마지막 검수 ID다(볼 수 없는 검수면 400). 비식별본은 한 문으로 읽고, 포털은 `limit=500`으로 부른다.
- **ASSETS-6 Runbook 11의 복구 절차** — 고침(문서). 스토리지 장애로 검증이 FAILED(failed_from=VALIDATING)가 된 원 영상·센서 자산은 스토리지 복구 뒤 재처리로 등록·예약 정산까지 끝난다고 적었다. 그 밖의 유형은 여전히 409라 절차가 없다. ADR-0071 감수할 점에 이후 ADR-0076을 표시했다.
- **ASSETS-7 긴급 열람 거부 사유 코드(중복)** — ASSETS-2와 같은 지적이라 같은 커밋으로 고쳤다.

## 업로드

| id | 지적 | 심각도 | 판정 | 처리 |
|---|---|---|---|---|
| UPLOADS-1 | 시간 초과된 완료가 503을 받은 뒤 저장소 완료가 확인(HEAD)과 중단 사이에 끝나면 자산 없는 객체가 남고, 삭제 경로의 삭제 증명은 그 객체를 빠뜨린다 | 중간 → 낮음 | 확인 | 고침 (이 PR) |
| UPLOADS-2 | Runbook 11·ADR-0071이 저장소 읽기 실패로 FAILED(failed_from=VALIDATING)가 된 자산에 복구 경로가 없다고 하지만, 같은 줄기의 S22(ADR-0076)가 재처리로 길을 더했다 | 낮음 | 확인 | 고침 (이 PR) |
| UPLOADS-R1 | 장치 토큰이 같은 기관의 매핑되지 않은 원천의 업로드 세션에 조용한 404를 받고 DEVICE_SCOPE_VIOLATION 감사가 없다 | 낮음 | 반박 | 계약이 매핑되지 않은 원천의 자원은 404라고 정했고, 장치 읽기는 404·제출만 DEVICE_SCOPE_VIOLATION이라는 모양을 2026-11-05 점검에서 이미 정했다 |
| UPLOADS-3 | 만료 업로드 정리가 멈춘 세션마다 저장소 호출을 시간 한도 없이 다시 해, 응답 없는 저장소가 매 주기 워커를 붙잡는다 | 낮음 | 확인 | 고침 (이 PR) |
| UPLOADS-4 | abortUpload 계약은 다른 요청이 처리 중인 세션에 409라 하지만, DELETE는 행 잠금을 기다린 뒤 204로 취소한다 | 낮음 | 확인 | 고침 (이 PR) |
| UPLOADS-R2 | ADR-0082는 에이전트가 장애 503 완료를 다시 보내 끝낸다고 하지만, 두 자사 클라이언트는 GET …/parts로 재개해 저장소가 이미 완료했으면 503에 머문다 | 낮음 | 반박 | ADR-0082의 그 문장은 저장소가 완료하지 않은 짧은 장애를 말하고 그 경우 에이전트는 끝낸다. 응답을 잃은 경우는 따로 적혀 있다 |
| UPLOADS-5 | Runbook 11이 FAILED(failed_from=VALIDATING) 자산은 되돌릴 길이 없다고 하지만, S22로 영상·센서 자산은 재처리로 검증을 다시 시작한다 | 낮음 | 확인 | 고침 (이 PR) |

- **UPLOADS-1 503 뒤 늦게 끝난 완료의 객체** — **고침** (ADR-0084, `test_upload_cleanup_late_completion.py`). 처음 확인에서 객체가 없으면 멀티파트 중단 뒤 다시 확인한다. 그래서 503으로 끝난 완료를 저장소가 늦게 마친 객체를 취소·만료 정리가 지우고, 자산·회차 삭제는 그 객체를 뺀 증명 대신 409로 미룬다. 다시 확인이 실패하면 503이다. ADR-0065 결정 2·3 문장도 바로잡았다.
- **UPLOADS-2 Runbook 11·ADR-0071의 '복구 절차 없음'** — **고침** (문서). 바이너리가 있는 영상 원본·클립과 센서 파일 자산은 스토리지 복구 뒤 `POST /v1/assets/{id}/reprocess`(ADR-0076)로 검증부터 다시 하게 적었다. 그 밖의 유형만 절차가 없다고 남겼다.
- **UPLOADS-3 만료 정리 한 번의 시간 한도** — **고침** (ADR-0084, `test_upload_cleanup_late_completion.py`). `sweep_expired`가 60초를 넘기면 남은 후보를 다음 주기로 넘기고 `expired_upload_sweep_budget_spent`를 남긴다. 응답 없는 저장소에서 한 번이 '한도 + 후보 하나의 시간 초과'로 묶인다. 설정으로 둘지는 사람 결정이다.
- **UPLOADS-4 abortUpload 계약의 '처리 중이면 409'** — **고침** (계약 설명, ADR-0084, `test_upload_cleanup_late_completion.py`). 다른 요청이 쥐고 있으면 끝나기를 기다린 뒤 판단한다(끝냈으면 409, 되돌려졌으면 204)고 적었다. 동작 변경은 없다.
- **UPLOADS-5 Runbook 11 재처리 절차** — **고침** (UPLOADS-2와 같은 커밋). Runbook 11 '이 문서의 대상이 아닌 것'에 재처리 절차(PLATFORM_OPERATOR, 다중 인증, 사유 필수, job_ids는 검증 작업)를 적었다. ASSETS-6이 같은 곳을 고쳐, 두 문구를 한 목록으로 합쳤다.

## 보고서

| id | 지적 | 심각도 | 판정 | 처리 |
|---|---|---|---|---|
| REPORTS-1 | HWPX 양식 검사가 zip 심볼릭 링크 항목을 받고, 내보내기가 그대로 옮긴다 | 낮음 | 확인 | 고침 (이 PR) |
| REPORTS-O1 | PDF 렌더 한도가 프로세스당 풀 하나라, 한 기관이 다른 모든 기관을 429에 묶어 둘 수 있다 | 낮음 | 확인 | 사람이 정할 것 |
| REPORTS-2 | exportReport 요약이 아직 `/v1/capabilities`를 가리킨다(설명과 실제 경로는 `/v1/system/capabilities`) | 낮음 | 확인 | 고침 (이 PR) |
| REPORTS-R1 | 마이그레이션 0025 downgrade가 버전별 기간을 말없이 잃고, ADR 되돌리기 절이 이를 말하지 않는다 | 낮음 | 반박 | downgrade 주석이 버전별 기간을 버린다고 밝히고, ADR 감수할 점이 다시 채우는 방식을 적었다. 저장소의 일반적인 downgrade 관례다 |

- **REPORTS-1 HWPX 양식의 심볼릭 링크 항목** — **고침** (ADR-0066 갱신, 단위·통합 `test_report_export_hardening.py`). 외부 속성에 심볼릭 링크·장치·FIFO·소켓을 적은 항목이 든 양식은 만든 OS 표시와 상관없이 PUT 400이고, 이미 저장된 것은 내보내기 409다. 내보낸 HWPX 항목의 머리글은 이름·순서·시각·압축 방식만 옮겨 새로 만든다(일반 파일 0o644·폴더 0o755, 추가 필드 없음). 고치면서 유니코드 경로 추가 필드(0x7075) 때문에 내보낸 항목 이름이 '../../evil.xml'로 읽히던 길도 같이 막았다.
- **REPORTS-2 내보내기 요약의 기능 상태 경로** — **고침** (`tests/contract/test_contract_text_paths.py`). exportReport 요약이 설명·실제 라우트와 같은 `/v1/system/capabilities`를 가리킨다. 계약·스키마 글에 적힌 '/v1/…' 경로가 모두 계약 경로인지 시험이 본다. 생성 클라이언트는 다시 만들어야 한다.

## 계약 검증

| id | 지적 | 심각도 | 판정 | 처리 |
|---|---|---|---|---|
| VALIDATE-1 | 검증기 메모리 폭증: 큰 geometry의 좌표 하나가 틀리면 문서 1MB에 약 1.6GB를 써, 본문 상한 20MiB 안에서 요청 하나로 메모리가 바닥난다 | 높음 | 확인 | 고침 (이 PR) |
| VALIDATE-2 | V9가 큰 location과 스키마 오류가 있는 이벤트 문서의 검증 CPU를 약 두 배로 늘린다. ADR-0067의 '6초 → 7초'는 낮게 적었다 | 중간 → 낮음 | 확인 | 고침 (이 PR) |
| VALIDATE-3 | 이벤트 등록·수정이 사람이 아닌 주체의 human_verified를 403으로 거부하지만 PERMISSION_DENIED 감사가 없다 | 낮음 | 확인 | 고침 (이 PR) |
| VALIDATE-4 | validate_document가 경고 100개 뒤의 ERROR를 잘랐을 때 사전 검증은 valid:true인데, 운영 경로는 그 항목을 거부한다 | 중간 | 확인 | 고침 (이 PR) |
| VALIDATE-R1 | 계약이 validateContract 판정이 호출 기관의 하위 코드 등록에 달려 있다는 것을 말하지 않는다 | 낮음 | 반박 | 계약은 사전 검증이 보는 규칙·문제 코드를 나열한 적이 없고, 상태·필드·헤더가 바뀌지 않았다. 이벤트 제출도 등록되지 않은 하위 코드를 422로 거부한다 |

- **VALIDATE-1 큰 geometry의 좌표 하나로 메모리 폭증** — **고침** (ADR-0086, `test_contract_validator_cost.py`). 계약 검증기의 oneOf가 맞지 않은 가지의 오류(context)를 모으지 않고, 표준과 같은 오류 하나를 낸다. 좌표 하나가 틀린 2만 좌표 이벤트 항적의 RSS 증가가 475MB에서 2MB로 줄었고, 판정·문제 목록은 같다. 좌표 수 상한(계약 maxItems 또는 서버 상한)은 사람 결정으로 남는다.
- **VALIDATE-2 이벤트 위치 오류에 location을 되풀이해 검사** — **고침** (ADR-0086, `test_contract_validator_cost.py`). unevaluatedProperties 보정은 표준을 한 번만 돌리고, 위치 재검사는 계약의 location 속성 스키마로만 하며 깊은 복사를 하지 않는다. 같은 문서 CPU 14.6초 → 2.7초(좌표 스키마 적용 13n → 3n번). ADR-0067 결과의 수치도 바로잡았다.
- **VALIDATE-3 이벤트 확인 표시 거부의 감사 누락** — **고침** (`test_event_verify_denial_audit.py`). 수집기·API 키의 human_verified 403(등록·PATCH)이 PERMISSION_DENIED 감사를 요청과 따로 확정해 남긴다. 응답은 그대로다.
- **VALIDATE-4 사전 검증이 경고 뒤로 잘린 ERROR를 놓침** — **고침** (ADR-0086, `test_contract_prevalidate_rules.py`·`test_contract_validator_rules.py`). 목록에 ERROR가 없는데 잘렸으면 운영 경로의 ITEM_PROBLEM_LIMIT 목록으로 다시 판정한다(그래도 잘리면 무효). 보여 주는 목록과, 앞 목록만으로 판정이 정해지는 문서의 비용은 그대로다.

## 범위 메모 분류

각 항목의 범위 메모에 '고치지 않음'으로 적힌 것을 다시 본 결과다. 범위 메모·ADR에 이미 남은 일로 적힌 것은 새 결함이 아니라서 반박으로 두었다.
반박이어도 실제 동작은 맞는 것이 많으니, 남은 일 목록은 각 항목의 범위 메모와 HANDOFF를 따른다.

| id | 지적 | 심각도 | 판정 | 처리 |
|---|---|---|---|---|
| FOLLOWUPS-D1 | data_role과 session_id를 함께 보낸 PATCH /v1/assets/{id}에서 회차 검사가 실패해 CORE 변경이 되돌려져도 이용기관 철회 메일은 이미 나간다 | 중간 | 판정 갈림 | 판정 갈림 — 사람이 정할 것 |
| FOLLOWUPS-D2 | 공유 등급 신청 하향과 공유 심사 WITHDRAW의 이용 권한 철회에 GRANT_REVOKED 감사가 없다(SHARING-D1과 같은 지적) | 중간 | 판정 갈림 | 판정 갈림 — 사람이 정할 것 |
| FOLLOWUPS-R1 | 회차 보고서가 회차 시작이 9999-12-31T15:00Z 뒤면 500을 낸다(S31의 나머지) | 낮음 | 반박 | rpa 범위 메모가 범위 밖 새 지적 후보로 이미 적었다 |
| FOLLOWUPS-R2 | 요청 안에서 만드는 데이터셋 버전이 data_card 422 때 DATASET 버킷에 복사한 객체를 남긴다 | 낮음 | 반박 | dsa 범위 메모 4가 원래 있던 범위 밖 동작으로 이미 적었다 |
| FOLLOWUPS-R3 | 장치의 human_verified에 대한 POST/PATCH /v1/events 403이 PERMISSION_DENIED 감사를 남기지 않는다 | 낮음 | 반박 | shr 범위 메모가 S14 같은 유형으로 적어 둔 범위 밖 일이다. 같은 문제는 VALIDATE-3으로 확인돼 이 PR에서 고쳤다 |
| FOLLOWUPS-R4 | createSessionRecord·createAccessRequest가 Idempotency-Key를 받지만 계약에 409가 없다 | 낮음 | 반박 | shr 범위 메모가 S15 같은 유형으로 이미 적었다 |
| FOLLOWUPS-R5 | 클립 요청 중복 확인이 최신 클립 작업 100개만 봐서, 같은 구간의 오래된 활성 작업이 다시 큐에 들어간다 | 낮음 | 반박 | asb 범위 메모 3(b)가 남은 일로 이미 적었다 |
| FOLLOWUPS-R6 | 읽을 수 없는 `to`가 `/from` 자리로 보고된다(POST /v1/reports, 보충 수집) | 낮음 | 반박 | rpa·rec 범위 메모가 이미 적었다 |
| FOLLOWUPS-R7 | POST /v1/share-candidates가 `asset_ids: null`을 받는다(계약은 minItems 1인 배열) | 낮음 | 반박 | shr 범위 메모가 'S16 이웃'으로 이미 적었다 |

## 사람이 정할 것

- **SHARING-O1·O2 분류 지정으로 끊긴 권한의 사유 문구.** 이용기관이 보는 철회 사유를 고정 문구(예: 운영자 분류 변경에 따른 하향)로 바꾸고 운영자 사유는 감사에만 둘지, 지금처럼 보여 주고 계약·ADR-0074에 적을지.
- **SHARING-O3 data_card 스키마 위반의 응답.** 요청 모델에서 검사해 400 VALIDATION_ERROR로 할지, 422 RULE_VIOLATION을 계약 설명에 적을지.
- **SHARING-D1·FOLLOWUPS-D2 공유 등급 신청 하향·WITHDRAW의 GRANT_REVOKED.** 권한마다 GRANT_REVOKED를 남길지. asa 범위 메모와 ADR-0076 감수할 점에 남은 일로 적혀 있다.
- **FOLLOWUPS-D1 되돌려진 CORE 변경의 철회 메일.** 회차 검사를 data_role 처리보다 먼저 할지, 알림을 커밋 뒤로 미룰지(outbox).
- **ASSETS-O1 같은 회차 재검수와 L2 이상 비식별본.** 공유 등급을 내릴지, 그런 비식별본을 건너뛸지, 재검수 표시를 다른 곳에 둘지(domain.md 4장과 14번 요구사항 4.4 사이의 결정).
- **REPORTS-O1 PDF 렌더 한도.** 기관마다 몫을 나눌지, 프로세스 하나의 풀을 ADR-0066 감수할 점에 적고 둘지.
- **좌표 수 상한(VALIDATE-1 뒤).** 계약 좌표 배열에 maxItems를 둘지, 서버 상한을 둘지, 값을 얼마로 할지. 좌표 하나에 약 0.1ms라 20MiB 항적이면 요청 하나가 1~2분이다.
- **사전 검증의 배치 항목 수 한도.** 사전 검증도 `OBSERVATION_BATCH_MAX`(운영 413)를 볼지.
- **만료 업로드 정리의 시간 한도.** 지금 함수 기본값 60초인 `budget_s`를 설정으로 둘지. 정상 저장소에서 100건이 60초 안에 끝나는지는 운영 저장소에서 재지 않았다(확인 필요).
- **영상·센서 밖 유형의 FAILED(failed_from=VALIDATING) 복구.** 재처리가 409를 주는 유형에 복구 경로를 둘지.
- **저장소 안에서 완료와 중단이 겹칠 때 SeaweedFS 동작.** 확인 필요.
- **범람 웹훅 구독 쉬게 하기.** 구독 단위로 시도를 묶을지(실패 뒤 쉬기, 연속 실패로 끄기)를 W5(비활성 의미)와 함께 정할지.
- **클립 전용 워커의 주기 작업.** 전용 워커가 정비·감시·웹훅 전송을 건너뛰게 하는 스위치(WORKER_PERIODIC 같은 설정)를 둘지. 지금은 두 워커를 함께 다루라고 runbook에 적었다.
- **HWPX 내보내기 메타데이터.** 한글이 항목 권한·추가 필드를 쓰는지, 실제 관리기관 양식(T11-f)의 외부 속성 값, 풀기 도구별 링크 처리는 확인 필요.
