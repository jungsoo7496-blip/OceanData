# ADR-0088: 공유 묶음 통합 검토 보완 — 재검수 잠금 순서, 제공기관의 데이터셋 보기, 이용 목록 쪽 나눔, ID 목록 검사

- 상태: accepted
- 날짜: 2026-11-20
- 관련: 13번 요구사항 4.6(분류 지정)·7.1(카탈로그·샘플)·8·9(신청·권한)·11.1(이용 이력), 14번 요구사항 4.4(같은 회차 재검수),
  domain.md 2장(Tenant_Guard 표), api-conventions.md(목록 커서), ADR-0053(교착·직렬화 실패의 409), ADR-0072(데이터셋 목록),
  ADR-0074(분류 지정의 계보 잠금), ADR-0075(같은 회차 재검수 표시), ADR-0077(클립 목록 커서), 2026-11-20 통합 검토 SHARING-1~5

## 맥락

2026-11-12·13 계약 점검의 고침을 한 줄기로 합친 뒤 통합 검토에서, 각 항목만 볼 때는 보이지 않던 빈틈이 확인됐다.

1. **SHARING-1 재검수와 분류 지정의 교착**: ADR-0074 결정 4와 `sharing.lock_family` 설명은 분류 지정과 비식별 검수 결정(`deid.decide`)이
   같은 순서(원본 → 비식별본)로 잠가 교착하지 않는다고 적었다. 한 계보 안에서는 맞다. 그런데 같은 회차의 미탐 결정은 `_recheck_session`에서
   다른 원본 B의 비식별본 B' 행을 잠그고(계약 점검 S20), 원본 B는 잠그지 않은 채 `original_asset_id=B`인 재검수 행을 넣는다. 그 외래 키 확인은
   B에 KEY SHARE 잠금이 필요하다. 같은 때 B의 분류 지정은 B를 FOR UPDATE로 잠근 채 B'를 기다린다. 서로 기다려 PostgreSQL이 한쪽을 끊고,
   분류 지정이면 ADR-0074가 없앴다고 한 409 CONFLICT, 결정이면 검수자의 결정이 실패했다. 수정 전 main에서는 분류 지정이 그 행만 고쳐
   (FOR NO KEY UPDATE, KEY SHARE와 충돌하지 않음) 이 고리가 없었다.
2. **SHARING-2 제공기관에게 보이는 다른 기관 버전**: 제공기관 주체(ORG_ADMIN·ORG_MEMBER)는 버전 하나에라도 자기 기관이 들어 있으면 그 데이터셋을 연다.
   그런데 보기는 `visible_versions(consumer=False)`로 모든 버전을 돌려줘, 다른 기관 자산만 든 버전의 항목 수·크기·매니페스트 해시·등급, 최신 버전의
   데이터 카드(다른 기관 것이어도), 최신 READY 버전에서 뽑은 샘플 썸네일의 단기 URL(다른 기관 콘텐츠)까지 보였다. 게시 전에도 그랬다.
   domain.md 2장 표는 ORG_ADMIN·ORG_MEMBER에게 메타데이터·바이너리 모두 자기 기관만 허용한다. 같은 버전의 계보 조회는 이미 404였다.
3. **SHARING-3 쪽을 나누지 않는 이용 목록**: 계약 점검 S03은 데이터셋 목록만 나눴다. 같은 라우터의 `GET /v1/access-requests`(운영자는 모든 기관),
   `GET /v1/grants`, `GET /v1/usage-history`는 쌓인 행을 한 응답에 모두 담았고 계약에도 `limit`·`cursor`가 없었다(api-conventions.md는 모든 목록을
   커서로 나누고 기본 50, 최대 500이라고 한다). 이용 이력은 신청마다 결정·권한·다운로드를 따로 읽어 신청 수에 비례해 문이 늘었다.
4. **SHARING-4 공유 후보 지정의 ID 형식**: 계약 `markShareCandidates`는 `asset_ids` 항목과 `session_id`를 common.schema.json의 ID 형식으로 적었는데,
   모델은 `list[str]`·`str`이라 형식이 틀린 ID가 `load_scoped`까지 가서 404 NOT_FOUND였다. 같은 묶음의 데이터셋 버전 `item_asset_ids`는 S05에서
   이미 400이 됐다.
5. **SHARING-5 중복 ID 오류의 두 모양**: 같은 `uniqueItems` 위반에 데이터셋 버전(S05)은 다시 나온 자리마다 `/item_asset_ids/<i>`를, 공유 후보(S16)는
   배열 자리 `/asset_ids` 하나를 줬다. ADR-0072는 S05가 웹훅 `event_types`·재처리 `steps`와 같은 방식이라고 적었지만, 그 둘(main에 이미 있던 것)은
   배열 자리 하나(`/event_types`·`/steps`)다. 매니페스트·장비 정책 같은 문서를 검사하는 JSON Schema 검사기(`contracts.schema_problems`)도
   `uniqueItems`를 배열 자리에 적는다.

## 결정

1. **재검수가 여는 다른 원본의 비식별본도 원본 → 비식별본 순으로 잠근다** (`deid._recheck_session`). 열린 검수가 없는 형제마다
   `db.get(Asset, 원본, with_for_update=True)` 다음에 비식별본을 잠그고 다시 확인한다. 형제는 원본 ID → 비식별본 ID 순으로 돈다.
   이제 결정이 잠그는 모든 계보에서 원본이 비식별본보다 먼저라, 분류 지정(`lock_family`)·삭제(`retention.with_descendants`)와 같은 순서다.
   분류 지정은 계보 하나만 잠그므로 결정과 교착하지 않는다. `lock_family` 설명과 ADR-0074 결정 4에 이 경로를 적었다.
   버린 대안:
   - 원본을 `SKIP LOCKED`·`NOWAIT`로 잠그고 잠겨 있으면 그 형제를 건너뛰기 — 재검수를 나중에 다시 여는 경로가 없어 요구사항 4.4의 표시가 빠진다.
   - 결정 전에 회차의 형제를 모두 찾아 원본 전부 → 비식별본 전부를 ID 순으로 한꺼번에 잠그기 — 결정끼리의 교착까지 없앨 수 있지만,
     결정 함수의 잠금 위치와 형제를 읽는 시점이 바뀌는 큰 변경이고 ADR-0075의 '잠근 뒤 다시 확인'을 다시 짜야 한다.
   - 문서만 고치고 409를 받아들이기 — 같은 회차를 검수하는 동안 운영자의 분류 지정이 늘 겹칠 수 있는 자리라 재현이 쉽다.
2. **제공기관은 자기 기관 자산이 든 버전만 본다** (`api/datasets.py::_visible`). 운영자·이용기관이 아닌 주체에게는 `provider_organization_ids`에
   자기 기관이 있는 버전만 남기고, `data_card`(그 목록의 최신)와 `samples`(그 목록의 최신 READY)도 이 목록에서 고른다. 상세(`getDataset`)와
   목록(`listDatasets`, ADR-0072 쪽 나눔)이 같은 함수를 쓴다. 계보 조회의 '제공기관은 자기 항목만'과 같은 규칙이다. 응답 형태는 그대로이고 계약
   두 작업의 설명에 적었다.
   버린 대안: 모든 버전을 두고 다른 기관 버전의 카드·샘플만 빼기 — 버전 목록의 크기·해시·등급도 다른 기관 메타데이터다.
3. **세 목록을 계약 먼저 `limit`·`cursor`로 나눈다.** 세 작업에 `Cursor`·`Limit` 매개변수, 400, 응답의 `next_cursor`(필수, 마지막 쪽이면 null)를 더했다.
   - 이용 신청·이용 권한: `paginate`로 `access_request_id`·`grant_id` 순이다. ID가 ULID라 만든 순서와 같다(전에는 `created_at`, ID 순). 역할별 조회
     (`_requests_for`·`_grants_for`)는 그대로이고 정렬만 뺐다.
   - 이용 이력: 사건 하나가 항목 하나인 응답 형태는 그대로 두고, 다섯 종류의 사건(신청·결정·권한·다운로드·철회/만료)을 SQL `UNION ALL` 하나로
     모아 (초 단위 at, kind, 사건 ID) 순으로 자른다. 전에 파이썬에서 정렬하던 (at, kind) 순서와 같고, 같은 시각·종류 안에서는 사건 ID로 정해진다.
     커서는 앞 쪽 마지막 사건의 [at, kind, 사건 ID]다(클립 목록 ADR-0077과 같은 방식). 쪽마다 문은 사건 질의 하나와 그 쪽 이용기관 이름 하나다.
   - 웹(`Sharing.tsx`)은 세 목록을 `apiAll`로 `next_cursor`를 따라 끝까지(500개씩, 최대 20쪽) 읽는다. 대기 중인 신청이 뒤쪽에 있어 첫 쪽만
     읽으면 빠지기 때문이다.
   버린 대안:
   - 이용 이력을 신청 단위로 나누기(커서 = 신청 ID) — 간단하지만 쪽마다 시간 순서가 다시 시작해 이어 붙인 순서가 지금과 달라진다.
   - 신청마다 사건을 묶은 응답으로 바꾸기 — 계약의 항목 형태가 바뀌어 클라이언트가 깨진다.
   - 웹은 `?limit=500`만 부르기(S03 데이터셋과 같게) — 신청·권한은 오래된 것부터 나오므로 500개를 넘으면 새 신청(결정할 것)이 화면에서 빠진다.
4. **공유 후보 지정도 ID 형식을 모델에서 검사한다.** `asset_ids` 항목과 `session_id`에 계약과 같은 정규식을 걸어 400 VALIDATION_ERROR
   (`/asset_ids/<i>`·`/session_id`)를 준다. 정규식은 `domain.ids.id_pattern(kind)` 한 곳에서 만들고, 데이터셋 버전의 `ASSET_ID`도 이것을 쓴다.
   형식은 맞는데 없거나 다른 기관인 ID는 지금처럼 404(다른 기관이면 교차 기관 감사)다. 계약은 이미 이 형식이라 바꾸지 않았다.
5. **`uniqueItems` 위반은 배열 자리 하나로 답한다.** `errors.duplicate_items(field, values, what)`이 `{"pointer": "/<field>", "code": "unique"}`
   하나를 만들고, 다시 나온 값을 처음 나온 순서로 다섯 개까지 메시지에 적는다("자산 ID가 중복됐습니다: ast_…, ast_… 외 N개"). 공유 후보와 데이터셋
   버전이 이것을 쓴다. 웹훅·재처리는 이미 같은 포인터·코드라 손대지 않았다. S05의 자리별 포인터는 main에 나가기 전 이번 묶음에서 생긴 것이라
   이것만 바꾸면 이미 나간 동작은 그대로다. ADR-0072 결정 4의 틀린 문장을 바로잡았다.
   버린 대안: 네 곳 모두 자리마다(`/<field>/<i>`) — 어느 자리인지 포인터로 바로 보이지만, 이미 나간 웹훅·재처리의 포인터가 바뀌고 JSON Schema
   검사기의 모양과도 달라진다. 자리를 찾는 데 필요한 값은 메시지에 있다.

## 결과

좋아지는 점: 같은 회차의 미탐 결정과 다른 원본의 분류 지정이 겹쳐도 하나가 다른 하나를 기다렸다가 둘 다 끝난다. 두 기관이 함께 든 데이터셋에서
제공기관이 다른 기관 버전의 메타데이터·카드·썸네일을 받지 않는다. 이용 목록 세 개의 응답 크기와 문 수가 쪽 크기에 묶인다. 같은 계약 형식의
ID 목록 두 개(공유 후보·데이터셋 버전)가 형식 오류에 같은 400으로 답하고, 중복 ID 오류는 API 전체에서 한 모양이다.

감수할 점:
- 형제 원본 행도 결정이 끝날 때까지 잠긴다. 그동안 그 원본의 공유 심사·분류 지정·삭제가 기다린다(결정은 짧다).
- 여러 계보를 차례로 잠그는 경로끼리는 원본 순서가 엇갈리면 여전히 교착할 수 있다. 예: 같은 회차에서 원본마다 통과한 비식별본이 둘 이상이고
  두 미탐 결정이 서로의 형제 원본을 반대 순서로 잡을 때, 결정 중인 원본을 일괄 삭제가 다른 순서로 잡을 때. 결정 중인 비식별본 자신은 열린
  검수로 걸러져 잠그지 않으므로 흔한 경우(검수자 둘이 같은 회차를 동시에 미탐 결정)에는 생기지 않는다. 생기면 ADR-0053대로 409(Retry-After)다.
- 여러 기관이 함께 든 버전은 지금처럼 그 기관들 모두에게 보인다(버전 단위다). 그 버전의 카드·샘플에 다른 기관 항목이 섞이는 것은 버전을 함께
  만든 결과이고, 항목 단위로 나누지 않는다.
- 이용 신청 목록은 항목마다 권한·버전·승인 수를 읽는 문(`request_view`)이 그대로라, 쪽 크기 500이면 문이 1,500개쯤이다. 전에는 전체 행 수만큼이었다.
- 이용 이력 질의는 그 제공기관의 사건 전체를 모아 정렬한 뒤 자른다(색인으로 건너뛰지 못한다). 응답 크기와 메모리는 쪽 크기로 묶인다.
- `limit`을 주지 않는 클라이언트는 이제 첫 50개만 받는다. 웹은 끝까지 따라가고, 20쪽(1만 개)을 넘으면 그 뒤는 화면에 나오지 않는다.
- 중복 ID 오류의 포인터는 배열을 가리키므로 몇 번째 자리인지는 메시지의 ID로 찾아야 한다.

되돌리는 방법: (1) `_recheck_session`의 원본 잠금 줄을 `db.get(Asset, 원본)`으로 되돌리고 정렬을 비식별본 ID 순으로 돌린다. (2) `_visible`이
`visible_versions` 결과를 그대로 돌려주게 하고 계약 두 작업의 설명을 지운다. (3) 세 목록의 `page_params`·`paginate`·이력 질의를 지우고 계약의
매개변수·400·`next_cursor`를 되돌린 뒤 웹 `apiAll`을 `api`로 돌리고 생성 클라이언트를 다시 만든다. (4) `Candidates`의 두 `pattern`을 지운다.
(5) `duplicate_items` 대신 S05의 자리별 오류 함수를 되살린다. 마이그레이션 없음.

## 시험

`tests/integration/test_classification_family_locks.py::test_session_recheck_and_classification_of_another_original_do_not_deadlock` —
회차 SES_A에 결정할 비식별본 A'와 통과한 비식별본 B'를 두고, 결정이 B' 행을 잠그는 순간 B 분류 지정을 보내 잠금을 기다리게 한 뒤 결정을 끝낸다.
둘 다 성공하고 B'에 재검수가 열리며 B는 SECURITY다. 수정 전 코드에서 분류 지정이 409 CONFLICT("동시에 들어온 다른 요청과 겹쳐…")를 받아
`assert ('ok', 409) == ('ok', 200)`로 실패하는 것을 확인했다.

`tests/integration/test_dataset_provider_view.py::test_provider_sees_only_versions_with_own_assets` — v1.0.0(B 기관)과 뒤에 만든 v2.0.0(A 기관만,
READY, 샘플 있음)인 게시 전 데이터셋을 B 관리자가 상세·목록으로 보면 v1만, B 카드, 샘플 없음이다. A는 v2와 샘플, 운영자는 둘 다 본다. 수정 전 코드에서
B가 두 버전·'A 전용 카드'·샘플 1개를 받아 실패하는 것을 확인했다.

`tests/integration/test_sharing_list_pages.py` — 신청·권한 51개는 기본 50개와 `next_cursor`, `limit=20`이면 운영자·이용기관·제공기관 모두 20·20·11로
겹치지도 빠지지도 않고, `limit` 0·501은 400이다. 이용 이력 28건은 `limit=4`로 이어 읽은 것이 한 번에 받은 것(시각·종류 순, 설명 형식 그대로)과 같고,
틀린 커서는 400(`/query/cursor`)이다. 신청이 2개에서 22개로 늘어도 이력 조회의 문 수 차이는 2 이하다. 수정 전 코드에서 네 시험이 모두 실패하는 것을
확인했다(`assert (51 == 50)` 두 번, `KeyError: 'next_cursor'`, `assert (60 - 10) <= 2`).

`tests/integration/test_share_candidates_body.py::test_malformed_ids_are_validation_errors` — 맞는 ID와 함께 보낸 `not-an-asset-id`는 400
(`/asset_ids/1`), 틀린 `session_id`는 400(`/session_id`)이고 아무 자산도 후보가 되지 않는다. 수정 전 코드에서 두 경우 모두 `assert 404 == 400`으로
실패하는 것을 확인했다.

중복 ID: `test_dataset_contract_checks.py::test_version_item_ids_follow_contract_format`(요청 안·작업 큐 경로 모두 `[("/item_asset_ids", "unique")]`,
메시지에 다시 나온 ID), `test_share_candidates_body.py::test_asset_ids_must_be_non_empty_and_unique`(메시지에 다시 나온 ID),
`tests/unit/test_duplicate_items.py`(오류 없음, 처음 나온 순서, 다섯 개 넘으면 '외 N개'). 수정 전 코드에서 데이터셋 버전은
`('/item_asset_ids/2', 'unique') != ('/item_asset_ids', 'unique')`로, 공유 후보는 메시지에 ID가 없어(`assert 'ast_…' in '자산 ID가 중복되었습니다'`)
실패하는 것을 확인했다.
