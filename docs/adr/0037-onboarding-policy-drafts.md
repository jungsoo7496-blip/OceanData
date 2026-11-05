# ADR-0037: 온보딩 장비 정책 초안 — 카드의 구조화된 값만 미리 채우고, 운영자 확인 후 set-policy와 같은 길로 적용

- 상태: accepted (연계 난이도 등급 기준은 미결 — 아래 '미결 사항')
- 날짜: 2026-11-05
- 관련: 09번 요구사항 3.2·3.3·3.4·작업 3, 03번 요구사항 1.3·1.5·작업 17(ADR-0028), domain.md 3장(collection_mode·data_role)·
  6장(source_timezone)·9장(local_copy_policy), 기준 문서 부록 A·19장, AGENTS.md 4·5번

## 맥락
요구사항 3.2는 카드의 장비 항목으로 원천 장비별 정책 초안(collection_mode, 허용 data_role, local_copy_policy, 기대 수집 주기,
전송 허용 시간대, source_timezone)을 만들라 하고, 3.3은 운영자가 확인한 초안만 적용하라고, 3.4는 '확인 필요' 항목과 연계 난이도
등급을 보이라고 한다. 지금까지 장비 정책은 관리 명령 `set-policy`(정책 파일 통째)로만 저장했고, 연계 카드(ADR-0028)는 분야 안이
자유 기록이다 — 정책을 움직이는 구조화된 값은 희망 수집 수준·현장 사본 정책·현장 보존 기간뿐이다.
정책은 수집기 단위로 배포된다(device-policy.schema.json: 수집기 → sources[] 원천 장비별 항목, transfer_windows는 최상위).
수집 서버는 장비 기록의 값을 쓴다(variant 허용 = devices.collection_mode, 관측값 시각 변환 = source_timezone, 정상성 판정 =
expected_interval_s).

## 결정
1. **초안 = 원천 장비 하나의 항목 제안** (새 표 `policy_drafts`, 마이그레이션 0023(병합 때 0022 Hot/Warm 뒤로), ID `pdr_`). 대상은 수집기에 매핑된 원천 장비 또는
   스스로 원천인 수집기 — 매핑이 없으면 422. 일곱 항목마다 상태를 둔다: `FROM_CARD`(카드 값, 카드 위치 JSON Pointer 함께),
   `ENTERED`(사람이 넣음, null은 '없음'), `NEEDS_CONFIRMATION`(확인 필요). 값 형식은 장비 정책 계약의 같은 항목으로 검사하고
   (계약도 그 항목을 `$ref`로 가리킨다) source_timezone은 실제 IANA 시간대인지 본다.
   상태 DRAFT → APPLIED | REJECTED. 장비마다 DRAFT는 하나(부분 고유 색인, 있으면 409). 고칠 때마다 `revision`이 오르고,
   고치기·적용은 보고 있던 revision을 보내야 한다(그 사이 고쳐졌으면 409 — 검토하지 않은 값이 적용되지 않게).
   이 상태 코드는 온보딩 작업 흐름의 것이라 domain.md에 넣지 않았다(AGENTS.md 4: domain.md 코드값은 사람 승인 없이 바꾸지 않음).
2. **미리 채우기는 구조화된 값 세 개만**: `collection_mode_requested` → collection_mode, `current_storage.local_copy_policy`,
   `current_storage.local_retention_days`. 자유 기록(시각 분야의 'GPS UTC', 발생량 메모 등)에서는 값을 뽑지 않는다 — 허용 data_role·
   기대 수집 주기·전송 허용 시간대·source_timezone은 사람이 넣거나 '확인 필요'로 남는다(속성 기반 시험: 자유 기록을 바꿔도 결과가 같다).
   카드가 다른 장비에 연결돼 있으면(그 장비도 그 수집기도 아니면) 422, 다른 기관 카드는 404(제공기관)·422(운영자).
   버린 대안: 카드 장비 항목에서 장비·매핑을 자동 생성 — 종류·모델이 자유 기록이라 장비 유형·수집기 관계를 지어내게 된다.
   장비 등록·매핑은 기존 장비 화면(작업 4)에서 하고, 마법사는 카드의 장비 항목을 참고로만 보인다.
3. **초안은 아무것도 바꾸지 않는다. 적용은 운영자만**: 만들기·고치기 = ORG_ADMIN(자기 기관)·PLATFORM_OPERATOR(`policy_draft:write`),
   조회 = 제공기관(자기 기관)·운영자(`policy_draft:read`), 적용·반려 = PLATFORM_OPERATOR + 다중 인증(`device_policy:write`).
   ORG_ADMIN은 자기 초안을 적용·반려할 수 없다(403). 반려는 사유 필수.
4. **적용 = set-policy와 같은 길**: `services/device_policy.store_policy`로 모았다 — 관리 명령 `set-policy`도 이것을 쓴다.
   지금 수집기 정책(저장본, 없으면 장비 표로 만든 기본 정책)에서 그 장비 항목만 초안 값으로 바꾸고(항목이 없으면 장비 기록으로
   만들어 추가), transfer_windows는 수집기 전체에 적용한다. '확인 필요' 항목은 지금 값을 그대로 둔다(정책·장비 기록 모두).
   결과는 계약 스키마·매핑(그 수집기에 매핑된 같은 기관 장비)·같은 기관 카드 검사를 통과해야 저장되고, 아니면 422(unmet_conditions)이며
   아무것도 쓰지 않는다. 수집기 행을 잠가 버전 번호를 직렬화하고(잠금 순서: 초안 → 장비 → 수집기), policy_version은 `pv-<버전>`.
   초안이 정한 항목은 장비 기록(collection_mode·local_copy_policy·local_retention_days·expected_interval_s·source_timezone)에도
   반영한다 — 서버 쪽 판정이 에이전트 정책과 어긋나지 않게. 정책 행에는 카드 ID를 남겨 카드 응답 `linked_policies`에 보인다.
5. **감사**: POLICY_DRAFT_CREATED(항목별 상태), POLICY_DRAFT_UPDATED(바뀐 항목 전후, 메모는 길이만), 적용 때 POLICY_CHANGED
   (set-policy와 같은 이름, 대상 수집기, 정책 일곱 항목의 전후·장비 기록 전후·초안 ID·사유)와 POLICY_DRAFT_APPLIED(상태 전후),
   현장 사본 정책이 바뀌면 DEVICE_LOCAL_COPY_POLICY_CHANGED(ADR-0028과 같음), 반려 때 POLICY_DRAFT_REJECTED(사유, 상태 전후).
   성공 감사는 같은 트랜잭션으로 남긴다.
6. **미리 보기**: 상세·만들기·고치기 응답의 `preview`가 지금 정책과 적용 후 값(일곱 항목), 바뀌는 항목, 지금 정책에 그 장비 항목이
   있는지, 적용을 막는 조건(blockers)을 조회 시점에 계산한다. 운영자 화면은 이것으로 비교한 뒤 검토한 revision으로 적용한다.
7. **확인 필요 항목**(3.4): 연계 카드 응답에 `open_items`(부록 A 15개 분야 중 비어 있는 분야 EMPTY, 값에 '확인 필요'가 적혔거나
   현장 사본 정책이 UNKNOWN인 분야 NEEDS_CONFIRMATION + 위치)를, 초안 응답에 `needs_confirmation`(아직 확인 필요인 항목)을 둔다.
8. **연계 난이도 등급**: 기준 문서는 '연계 카드 → 난이도 등급 → PoC → 확대'(19장 리스크 대응)만 말하고, 등급 체계·판정 기준은
   저장소 어디에도 없다. 등급을 지어내지 않는다 — 카드 응답 `difficulty_grade = {grade: null, criteria_defined: false,
   note: '기준 미정 — 확인 필요'}`, 화면도 그대로 보인다.

## 미결 사항 (확인 필요)
- **연계 난이도 등급의 체계와 판정 기준**: 등급 이름·단계 수, 어느 카드 항목(통신·망분리, 장비 종류, 시각 동기화, 연계 방식 등)을
  어떻게 쓰는지, 누가 매기는지(자동 계산 / 운영자 판정). 정해지면 계약 `DifficultyGrade.grade`에 코드값을 더하고
  (domain.md에 넣을지 함께 결정), 판정 규칙은 domain/에 한 곳만 둔다.
- 실제 연계 카드 양식(ADR-0028과 같음): 받으면 장비 항목을 구조화해 장비·매핑 생성 도움을 검토한다.
- watch_rules·수집기 timezone·heartbeat 등 초안 밖의 정책 값은 지금처럼 set-policy로 정한다.

## 결과
- 좋아지는 점: 카드에서 정책까지 '누가 무엇을 정했는지'(카드 값 / 사람 입력 / 확인 필요)가 항목마다 남고, 운영자가 지금 정책과
  비교한 값만 적용된다. set-policy와 초안 적용이 같은 검사·저장 길을 쓰고, 저장 때 버전이 직렬화된다.
- 감수할 점: 한 장비 초안의 전송 허용 시간대는 같은 수집기의 다른 장비에도 적용된다(미리 보기에 COLLECTOR로 표시).
  같은 수집기의 정책을 set-policy로 바꾼 직후에는 초안 미리 보기가 바뀐다(검토 때 다시 본다). 세 상태 이외의 흐름(철회 등)은 없다 —
  잘못 만든 초안은 운영자가 반려한다.
- 되돌리는 방법: 0023 down(초안 기록만 지운다 — 이미 적용된 정책 버전·장비 기록은 남는다), 경로·권한 제거.
  `store_policy`로 옮긴 set-policy 검사는 동작이 같다(출력 문구만 조건 목록으로 바뀜).
