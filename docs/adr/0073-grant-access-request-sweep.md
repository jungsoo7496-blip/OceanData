# ADR-0073: 이용 권한·이용 신청·배포의 교차 기관 감사, 신청 기간 날짜, 응답 기한 뒤 승인

- 상태: accepted
- 날짜: 2026-11-12
- 관련: 02번 요구사항 2.2(교차 기관 404·감사), 13번 요구사항 8.1·8.3·9.1·10.2, security.md, domain.md 4·5장(grant_status), ADR-0019(버전 등급 재평가), ADR-0053(요청 트랜잭션), 2026-11-12 계약 점검 S02·S06·S07·S09·S10

## 맥락

2026-11-12 계약 점검에서 이용 권한·이용 신청·배포 묶음의 빈틈 다섯 건이 확인됐다.
1. **S02·S06 교차 기관 감사**: `POST /v1/distributions`, `POST /v1/grants/{id}/revoke`, `POST /v1/grants/{id}/destruction-confirmation`,
   `POST /v1/access-requests/{id}/decisions`는 역할별 조회(`_grants_for`·`_requests_for`)로 행을 찾고, 없으면 바로 404를 냈다.
   `load_scoped`를 거치지 않아 다른 기관의 권한·신청 ID를 시도해도 `CROSS_ORG_ACCESS_ATTEMPT`가 남지 않았다(security.md는 남기라고 한다).
2. **S07 신청 기간 날짜**: `Period`는 정규식(`YYYY-MM-DD`)만 검사해 2026-02-30·2026-13-01·2027-01-00은 `date.fromisoformat`의 ValueError로,
   끝 날짜 9999-12-31은 '끝 날짜 다음 날 0시' 계산의 OverflowError로 500이었다. 계약은 `format: date`이고 형식 오류는 400이다.
3. **S09 L3 미달 422**: 제공기관 승인 뒤 자산 등급을 KEEP_UNTIL_EXPIRY로 낮추면 운영자 승인이 422 RULE_VIOLATION인데, 이 동작은 ADR-0019·domain.md대로이고
   계약 `decideAccessRequest` 응답 목록에만 422가 없었다.
4. **S10 응답 기한 뒤 승인**: `datasets.decide`는 `deadline_at`을 보지 않아, 기한이 지났어도 워커 정비 주기(`expire`, 기본 60초, 워커가 멈추면 무기한)가
   돌기 전이면 제공기관·운영자 승인이 받아들여져 GRANTED와 이용 권한이 생겼다. 요구사항 8.3은 기한 안에 승인이 없으면 거부라고 한다.

## 결정

1. **역할별 조회는 그대로 두고, 빠진 행만 주인을 읽어 감사한다** (`api/datasets.py::_not_found`).
   조회가 비면 기본 키로 행의 `organization_id`(Grant·AccessRequest 모두 이용기관)를 읽는다. 행이 있고 요청자 기관과 다르면 `load_scoped`와 같은 형식으로
   `CROSS_ORG_ACCESS_ATTEMPT`(result DENIED, organization_id=행의 이용기관, target_type `Grant`·`AccessRequest`, target_id, source_ip, details.path)를
   요청 트랜잭션과 따로 확정한다(요청은 404로 되돌려진다, ADR-0053). 응답은 없는 ID와 같은 404다. 없는 ID, 그리고 같은 기관인데 역할 범위 밖인 경우(예: 제공 자산이 없는
   이용기관 안의 ORG_ADMIN)는 감사하지 않는다 — `tenant.get_scoped`의 교차 기관 규칙(주인 기관 ≠ 요청자 기관)과 같다.
   버린 대안: `load_scoped(Grant)`로 바꾸기 — Tenant_Guard의 역할 필터는 CONSUMER만 가진 주체를 거부(false)하고, 제공기관 ORG_ADMIN에게는 자기 기관 행만 보여 준다.
   이용 권한은 이용기관 소유라 주인 이용기관과 자산을 낸 제공기관 모두 못 보게 된다.
2. **날짜는 모델에서 검사하고 문자열은 그대로 둔다.** `Period`에 `field_validator`를 더해 달력에 없는 날짜는 400 VALIDATION_ERROR(`/period/start_date`·`/period/end_date`),
   끝 날짜 `date.max`(9999-12-31)도 400(`/period/end_date`)이다. 이용 기간 끝은 끝 날짜 다음 날 0시(UTC)로 저장하므로 끝 날짜는 9999-12-30까지다.
   계약 `createAccessRequest` 설명에 적었다.
   버린 대안: (가) 필드를 `datetime.date`로 바꾸기 — `model_dump(mode="json")` 결과가 멱등 지문에 들어가 형태가 바뀔 수 있고, 느슨한 모드는 숫자(유닉스 시각)도
   날짜로 받아 `format: date`보다 넓어진다. (나) 서비스에서 ValueError·OverflowError를 잡아 400으로 — 목적 검사(422) 뒤라 검사 순서가 입력마다 달라지고 오류 위치를
   따로 만들어야 한다. (다) 끝 날짜 상한을 더 가깝게(예: 100년) 두기 — 근거가 되는 업무 규칙이 없다.
3. **S09는 계약만 고친다.** `decideAccessRequest`에 422(Unprocessable)와 설명 한 줄. 422일 때 운영자 결정은 기록되지 않고 신청은 PROVIDER_APPROVED로 남는다
   (오류 응답은 요청 트랜잭션을 되돌린다).
4. **기한이 지난 신청의 승인은 409로 거부하고, DENIED 전이는 정비 주기에 맡긴다.** `decide`에서 상태 검사 뒤, 결정 행을 쓰기 전에
   `approve and deadline_at < 지금`이면 409 INVALID_STATE_TRANSITION(`… (응답 기한 초과)`). 비교는 `expire`와 같은 엄격한 `<`라 기한 그 시각에는 둘 다
   아직 기한 안으로 본다. 거절(DENY)은 기한 뒤에도 받는다 — 끝 상태가 기본값 거부와 같고 실제 사유가 남는다. 계약 설명에 적었다(409는 이미 목록에 있다).
   버린 대안: 요청 안에서 바로 DENIED로 바꾸고 409 — 오류 응답은 요청 트랜잭션을 되돌리므로 전이를 따로 확정해야 하고, 승인 요청이 거부 결과를 만드는
   경로가 하나 더 생긴다. 200 + DENIED로 답하기 — APPROVE 요청에 거부를 성공으로 돌려주는 셈이라 클라이언트가 헷갈린다.

## 결과

좋아지는 점: 다른 기관의 이용 권한·신청 ID를 시도하면 다른 리소스와 같이 감사에 남는다. 잘못된 기간 날짜가 500이 아니라 위치가 붙은 400이다.
L3 미달 422가 계약에 있어 계약 시험과 생성 클라이언트가 이 응답을 안다. 기한이 지난 신청에는 정비 주기를 기다리지 않고도 권한이 생기지 않는다.

감수할 점: 범위 밖 요청마다 기본 키 조회가 한 번 더 있고, 다른 기관 요청에는 감사 행이 하나씩 생긴다. 기한이 지난 신청은 정비 주기가 돌 때까지
REQUESTED·PROVIDER_APPROVED로 보인다(승인은 409). 전에는 받던 끝 날짜 9999-12-31이 400이다.

되돌리는 방법: `_not_found` 호출 네 곳을 `ProblemError("NOT_FOUND")`로, `Period._calendar_date`와 `decide`의 기한 검사 줄을 지우고, 계약의 설명 세 줄과
`decideAccessRequest`의 422를 되돌린 뒤 생성 클라이언트를 다시 만든다. 마이그레이션 없음.

시험: `tests/integration/test_grant_access_request_tenant_audit.py`(네 작업의 다른 기관 요청 404·감사 한 줄씩, 없는 ID와 같은 본문·감사 없음, 같은 기관
역할 범위 밖은 감사 없음, 주인 요청은 그대로), `tests/integration/test_access_request_period_dates.py`(없는 날짜 4가지·9999-12-31은 400, 9999-12-30은 201),
`tests/integration/test_access_decision_below_l3.py`(422가 계약과 맞고 결정·권한이 남지 않음), `tests/integration/test_access_decision_deadline.py`
(기한 뒤 제공기관·운영자 승인 409, 거절은 200, 기한 그 시각 승인은 받음, 정비 주기 뒤 DENIED). 수정 전 코드에서 시험 네 개가 실패하는 것을 확인했다
(감사 0줄, 다섯 입력 모두 500, '계약에 422 응답이 없다', 기한 뒤 승인 200). 9999-12-30 시험은 수정 전후 모두 통과하는 경계 시험이다.
