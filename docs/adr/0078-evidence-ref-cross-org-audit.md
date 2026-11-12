# ADR-0078: 회차 기록 근거의 다른 기관 ID를 교차 기관 시도로 감사

- 상태: accepted
- 날짜: 2026-11-12
- 관련: 10번 요구사항 1.2(근거가 같은 기관 소속인지 확인), 02번 요구사항 2.2(교차 기관 시도 감사), 10번 design.md(다른 기관 근거는 '없음'과 같은 오류), `.kiro/steering/security.md` 테넌트 격리·감사 로그, ADR-0049(감사 행은 요청 단위로), ADR-0053(실패 감사는 따로 커밋), 계약 점검 S41

## 맥락

`POST /v1/sessions/{session_id}/records`와 `PATCH /v1/records/{record_id}`는 본문의 `evidence_refs`로 자산·이벤트·장비(관측 구간의 원천)를 가리킨다.
`evidence.check_refs`는 그 행의 `organization_id`를 직접 비교해 다른 기관 것이면 없는 ID와 같은 400 `VALIDATION_ERROR`(`code: not_found`)로 답했다.
응답으로 존재를 숨기는 것은 10번 설계대로지만 감사 행을 남기지 않았다. 그래서 기관 구성원이 근거 목록으로 다른 기관의 자산·이벤트·장비 ID를 넣어 보아도 기록이 없었다.
security.md는 교차 기관 접근 시도를 '반드시 기록'에 넣고, 경로나 본문으로 다른 기관 ID를 받는 다른 곳(`load_scoped`, 이벤트의 `linked_asset_ids`, 관측값 배치의 원천)은 `CROSS_ORG_ACCESS_ATTEMPT`를 남긴다.

## 결정

1. **응답은 그대로 둔다.** 다른 기관 근거와 없는 근거는 같은 400, 같은 `pointer`(`/evidence_refs/{i}`)·`code`·문구다. 경로 리소스처럼 404로 바꾸지 않는다 — 10번 설계가 근거 오류를 항목별 400으로 정했고, 404로 바꾸면 다른 근거의 오류를 함께 알려 줄 수 없다.
2. **행이 있고 소유 기관이 요청한 회차·기록의 기관과 다를 때만** 감사한다. 없는 ID, 같은 기관의 삭제된 자산은 감사하지 않는다. 다른 기관 자산은 삭제 표시가 있어도 감사한다(`get_scoped`가 소유 기관을 보는 방식과 같다).
3. 감사 행은 `load_scoped`와 같은 모양이다: `CROSS_ORG_ACCESS_ATTEMPT`, `result=DENIED`, `organization_id`=소유 기관(그 기관 관리자가 감사 목록에서 본다), `target_type`=`Asset`·`Event`·`Device`, `target_id`=가리킨 ID, `details.path`=요청 경로.
4. **대상마다 한 행**(같은 요청에서 같은 대상을 여러 번 넣어도 한 행)이다. `check_refs`는 그런 대상을 `BadEvidence.foreign`에 담고, 라우터의 `_check`가 감사한다. 서비스 함수는 감사·요청 정보를 모르는 채로 둔다.
5. 감사는 요청 트랜잭션과 **따로 커밋**한다(`connection`을 넘기지 않는다). 요청은 400으로 되돌려지므로 요청 트랜잭션에 넣으면 사라진다(ADR-0053).

버린 대안:
- 요청마다 한 행(첫 대상만 `target_id`, 나머지는 `details`) — ADR-0049의 '요청당 한 행'에 더 가깝고 쓰기 수가 줄지만, 감사 목록 API는 `details`를 보여 주지 않아 소유 기관 관리자가 나머지 대상을 볼 수 없다. 근거는 요청당 500개가 상한이고 그 요청은 이미 근거마다 조회를 하므로, 감사 쓰기도 같은 크기에 머문다.
- `check_refs`에 컨테이너·주체·요청을 넘겨 서비스 안에서 감사 — 근거 확인이 HTTP 맥락에 묶인다. 지금 부르는 곳은 두 경로뿐이고 둘 다 `_check`를 지난다.
- 근거를 `load_scoped`로 하나씩 읽기 — 첫 다른 기관 근거에서 404로 끝나 응답이 바뀐다(결정 1).

## 결과

좋아지는 점: 근거 목록으로 다른 기관 ID를 넣어 보면 소유 기관의 감사 기록에 남는다. 응답은 전과 같아 존재가 드러나지 않는다.

감수할 점: 다른 기관 근거가 많은 요청은 그만큼 감사 행을 쓴다(요청당 최대 500행, 감사 체인 잠금을 그만큼 잡는다). 요청 기관 관리자도 감사 목록에서 자기 구성원이 남긴 이 행(`actor_organization_id`)을 본다 — 경로 리소스의 교차 기관 감사와 같은 성질이고 이번에 바꾸지 않았다. 멱등 키가 같은 실패 요청은 응답을 저장하지 않으므로 다시 보내면 다시 감사한다.

되돌리는 방법: `app/services/evidence.py`의 `BadEvidence.foreign`·`other_org`와 `app/api/reports.py` `_check`의 감사 반복문을 지우고 `_check` 호출을 원래 인자로 되돌린다. 계약·마이그레이션 변경 없음.

시험: `tests/integration/test_record_contract_errors.py::test_cross_org_evidence_ref_is_hidden_and_audited` — 자산·이벤트·장비 근거 × 등록·수정 6가지에서 응답이 없는 ID와 같고, 없는 ID는 감사 0행, 다른 기관 근거(같은 것 두 번)는 소유 기관·대상 종류·ID·경로가 맞는 감사 1행이며 수정은 새 버전을 만들지 않는다. `test_own_org_deleted_asset_ref_is_not_audited` — 같은 기관 삭제 자산은 감사하지 않는다. 수정 전 코드에서 6가지 모두 감사 행이 0이라 실패하는 것을 확인했다(`assert 0 == 1`).
