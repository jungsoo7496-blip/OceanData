# ADR-0074: 분류를 더 엄격하게 바꾸면 공유 등급을 함께 내린다

- 상태: accepted
- 날짜: 2026-11-12
- 관련: 13번 요구사항 2.2(data_role CORE 변경의 하향)·3.6(가장 엄격한 값 상속)·4.6(운영자의 분류 지정)·10.3(하향과 기존 권한 처리), domain.md 3장(상속)·4장(공유 가능 조건, asset-metadata 불변식), ADR-0019(공유 판단 한 곳), ADR-0028(약정 종료는 공유를 자동으로 바꾸지 않음), ADR-0033(클립·삭제의 원 자산 → 파생본 잠금 순서), ADR-0053, security.md(감사 로그), 계약 점검 S11

## 맥락

`PATCH /v1/assets/{asset_id}/classification`(운영자)은 sensitivity_class·privacy_status만 바꾸고 sharing_scope는 보지 않았다.
1. L2(SANDBOX_PARTNERS)로 공유 중인 비식별본을 SECURITY로 지정하면 200을 돌려준 뒤에도 L2에 남았다. `GET /v1/assets/{asset_id}` 응답이
   계약 asset-metadata의 'L2 이상 등급의 불변식'(sensitivity_class ∈ {INTERNAL, RESTRICTED})을 어겼다.
2. 원본을 SECURITY로 지정하면 파생본은 원 자산의 가장 엄격한 민감도를 물려받아(domain.md 3장) 실효 민감도가 SECURITY인데, 등급은 L2 그대로였다.
3. 데이터셋 버전 등급과 이용 권한 판단은 저장된 sharing_scope를 본다. 그래서 운영자가 방금 보안 민감으로 지정한 데이터가 기존 이용기관에 계속 나갔다.
   공유 조건(Sharing_Rules)은 등급을 올릴 때만 평가했다. 처리 함수의 설명("더 엄격하게 바꾸면 파생본 등급은 규칙으로 다시 본다")과도 달랐다.

4. 이 동작으로 이미 'L2 이상인데 SECURITY'인 행(비식별본 자신, 또는 원본이 SECURITY인 파생본)이 운영 DB에 남아 있을 수 있다.

불변식은 지켜야 한다. 다만 어떻게 지킬지(시스템이 내릴지, 거부할지, 기존 권한을 어떻게 할지)는 정해진 것이 없었다.

## 결정

1. **분류를 바꾼 뒤 그 자산과 모든 파생본(계보 아래쪽)의 등급을 다시 본다.** 판단은 Sharing_Rules의 새 순수 함수
   `scope_after_change(current, before, after)` 한 곳에서 한다. 지금 등급 이하에서, 다음을 모두 만족하는 가장 높은 등급을 돌려준다.
   - **분류가 정하는 조건**(`CLASSIFICATION_CONDITIONS`: sensitivity_class ≠ SECURITY, sensitivity_class=INTERNAL, privacy_status 두 조건 —
     asset-metadata 'L2 이상 등급의 불변식'·L4 불변식과 같은 것)은 늘 바뀐 뒤 사실로 충족한다. 바꾸기 전부터 어긋나 있었어도(수정 전 코드가
     남긴 'L2인데 SECURITY' 행) 맞춘다. 그래서 운영자가 그런 행에 같은 분류를 다시 지정하면 바로잡힌다.
   - 그 밖의 조건(2단 약정, review_result, 데이터셋 버전 포함 등)은 이번 변경으로 새로 충족하지 못하게 된 것이 없어야 한다. 바꾸기 전 사실로도
     지금 등급에서 충족하지 못하던 것은 그대로 둔다(ADR-0028, 아래 버린 대안).

   예: L2 이상에서 SECURITY → L1, L4에서 RESTRICTED → L3(데이터셋 버전에 들어 있으면) 또는 L2.
   L0·L1 자산은 조건이 승인뿐이라 분류로 내릴 일이 없으므로 사실을 모으지 않는다(`sharing.shared_facts`).
2. **지금 등급보다 낮으면 시스템이 바로 내린다**(`sharing.downgrade`). 기존 이용 권한은 **즉시 철회(REVOKE_NOW)**다 — 그 자산이 든
   데이터셋 버전의 GRANTED 권한을 끊고 이용기관에 알린다. data_role을 CORE로 바꿀 때(요구사항 2.2, `PATCH /v1/assets/{asset_id}`)와 같은 처리다.
3. **감사.** 내린 자산은 `ASSET_CLASSIFIED` 감사의 `details.lowered`(자산 ID, 전·후 등급, 끊은 권한 ID `revoked_grant_ids`)와
   `details.existing_grant_handling`에 남긴다. 끊은 권한마다 수동 철회(`POST /v1/grants/{grant_id}/revoke`)와 같은 `GRANT_REVOKED` 행
   (대상 Grant, 기관은 이용기관, `details.cause=ASSET_CLASSIFIED`, `details.asset_id`=내린 자산)을 남긴다(security.md '이용 승인·철회').
   이 감사들은 요청 트랜잭션에 쓴다(`connection=db.connection()`, ADR-0053). 커밋이 실패해 하향·철회가 되돌아가면 그 기록도 남지 않는다.
   응답 본문과 상태 코드는 바꾸지 않는다.
4. **잠금 순서.** 분류를 바꾸기 전에 그 자산과 모든 파생본 행을 잠그고 다시 읽는다(`sharing.lock_family`). 순서는 원 자산 → 파생본이고, 계보
   단계마다 asset_id 순이다. 삭제(`retention.with_descendants`)·클립 기록(원 자산 행 먼저)과 같은 순서다. 비식별 검수 결정(`deid.decide`)은
   비식별본 → 원본 순으로 잠그고 있어서, 같은 원본의 분류 지정과 겹치면 교착했다(PostgreSQL이 한쪽을 끊어 분류 지정이 계약에 없는 409를 받음).
   그래서 결정도 원본 → 비식별본으로 바꿨다(잠그는 행과 하는 일은 같다).
   등급과 상관없이 파생본을 모두 잠근다 — 같은 때 들어온 등급 변경 신청이 바뀌기 전 분류로 L0·L1에서 L2 이상으로 올리고, 이쪽은 올리기 전 등급을
   보고 지나치는 일을 막는다(신청은 이 잠금을 기다린 뒤 바뀐 분류를 본다).
5. 계약 `classifyAsset`의 설명에 이 부수 효과를 적었다(계약 먼저).

버린 대안:
- **422 RULE_VIOLATION으로 거부하고 ORG_ADMIN이 먼저 내리게 하기** — 불변식은 지키지만, 운영자가 보안 민감 사실을 기록하지 못하고 그동안
  데이터는 계속 공유된다. 분류 지정은 운영자(`asset:classify`), 하향 신청은 ORG_ADMIN의 일이라 서로를 기다리게 된다.
- **지금 사실로 `max_scope_for`를 다시 계산해 그보다 높으면 내리기** — 분류와 상관없는 이유(2단 약정 만료 등)로도 등급을 내리고 권한을 끊는다.
  약정 종료는 공유·이용 권한을 자동으로 거두지 않기로 했다(ADR-0028). 분류 지정이 그 결정을 우회해서는 안 된다.
- **바꾸기 전부터 어긋난 조건은 분류 조건까지 모두 넘어가기**(첫 구현) — 수정 전 코드가 남긴 'L2인데 SECURITY' 행을 운영자가 같은 값으로 다시
  지정해도 200만 돌려주고 아무것도 고치지 않았다. ADR-0028의 예외는 분류와 무관한 사실(약정)을 위한 것이고, 분류 조건은 이 작업이 정하는 값이다.
- **L2 이상인 파생본만 잠그기** — 잠그는 행은 줄지만, 잠그지 않은 L0·L1 파생본을 같은 때 L2로 올리는 신청이 바뀌기 전 분류(예: 원본 INTERNAL)를
  보고 통과할 수 있다. 사실 수집만 L2 이상으로 줄였다(파생본 수에 비례하던 문 수가 사라진다).
- **`classifyAsset`에 409를 더하고 교착을 그대로 두기** — 같은 원본의 검수 결정과 분류 지정이 늘 한쪽 실패로 끝난다. 잠금 순서를 맞추는 편이
  작고 근본적이다.
- **기존 권한을 만료까지 유지(KEEP_UNTIL_EXPIRY)** — 보안 민감으로 지정된 데이터가 권한 기간 내내 나간다. 요청 본문으로 처리 방식을 받는 것은
  계약에 새 필드가 생기는 일이라 이번에는 하지 않았다(아래 감수할 점).

## 결과

좋아지는 점: 분류 지정 직후 그 자산과 파생본의 분류 조건(sensitivity_class·privacy_status)이 지금 등급과 맞는다 — asset-metadata 불변식 중
분류가 정하는 부분이다. 수정 전 코드가 남긴 어긋난 행도 운영자가 다시 지정하면 바로잡힌다. 보안 민감 데이터의 이용 권한이 바로 끊기고, 끊은 권한이
감사에 남는다. 다시 올리려는 신청도 바뀐 분류로 막힌다(기존 규칙). 분류 지정과 검수 결정이 같은 원본을 두고 겹쳐도 차례로 처리된다.

감수할 점:
- 운영자는 권한 처리 방식을 고를 수 없다 — 항상 즉시 철회다. L4 → L3처럼 이용기관 공유가 계속 되는 하향에서도 그 자산이 든 버전의 권한이 끊긴다
  (기존 하향 처리 `on_downgrade`가 버전 단위로 끊는다). domain.md는 하향 때 '이미 부여된 이용 권한의 처리(즉시 철회 / 만료까지 유지)를 함께
  정한다'고만 하므로, 기본값과 선택지를 둘지는 소유자가 정할 일로 남긴다.
- 분류와 무관한 불변식 조건(review_result=PASSED, share_status=APPROVED, legal_basis_ref 등)이 이미 어긋난 행은 이 작업이 고치지 않는다.
  분류 말고 다른 사실이 바뀌는 경로(검수 결과 변경 등)도 이번에 손대지 않았다.
- 하향은 자산별 별도 감사 행이 아니라 `ASSET_CLASSIFIED`(대상은 지정한 자산)의 `details.lowered`에 남는다. 파생본 ID로 찾으려면 details를 본다.
- 분류 지정은 자신과 모든 파생본 행을 잠근 채 끝까지 간다. 파생본이 많은 원본이면 그동안 그 행을 쓰는 다른 작업(검수 결정, 등급 신청, 삭제)이
  기다린다. 사실 수집은 L2 이상 자산만이므로 잠금 시간은 파생본 수에 거의 비례하지 않는다(문 수: 파생본 2개와 30개의 차이가 3 이하).
- 감사를 요청 트랜잭션에 쓰므로 감사 체인 잠금(`pg_advisory_xact_lock`)을 커밋까지 쥔다. 감사는 처리의 마지막 일이라 곧바로 커밋한다.
- 다른 여러 행을 쓰는 경로와 겹쳐 교착이나 직렬화 실패가 나면 ADR-0053의 매핑대로 409(Retry-After)가 될 수 있다. 다른 작업과 마찬가지로
  `classifyAsset`의 응답 목록에는 409를 따로 적지 않았다(공통 `Conflict` 설명).

되돌리는 방법: `classify_asset`의 잠금(`sharing.lock_family`)·`lower_after_classification` 호출·감사 details 두 항목과 `GRANT_REVOKED` 기록,
`sharing.lock_family`·`shared_facts`·`lower_after_classification`, `sharing_rules.scope_after_change`·`CLASSIFICATION_CONDITIONS`를 지우고
`downgrade`의 반환값을 없앤다. 계약 `classifyAsset`의 description을 지운 뒤 웹·Go 타입을 다시 만든다. `deid.decide`의 잠금 순서는 그대로 두어도
된다(원본 → 비식별본은 다른 경로와 같은 순서다). 마이그레이션 없음.

시험:
- `tests/integration/test_classification_lowers_scope.py` 6개: 비식별본 SECURITY → L1·권한 철회·불변식 계약 검사·`GRANT_REVOKED` 행, 원본 SECURITY →
  파생본 L1, 약정 만료만으로는 내리지 않음, 이미 SECURITY인 비식별본·원본에 다시 지정하면 바로잡음(2개), 커밋이 실패하면 분류·철회 감사가 남지 않음.
- `tests/integration/test_classification_family_locks.py` 3개: 검수 결정이 첫 행을 잠근 채 분류 지정이 기다리게 해도 둘 다 성공, L0 파생본 30개와
  2개의 문 수 차이 3 이하, 원본 → 프록시 → 썸네일을 계보 단계 순으로 잠금.
- `tests/unit/test_scope_after_change.py` 5개(속성 기반 시험 포함: 결과는 지금 등급 이하, 분류 조건은 늘 충족, 분류와 무관한 조건은 전부터 어긋난
  것만, 둘 수 있는 가장 높은 등급).

첫 구현에서 수정 전 코드로 통합 시험 3개가 모두 실패하고(등급이 SANDBOX_PARTNERS 그대로) 단위 시험은 함수가 없어 모이지 않는 것을 확인했다.
검토 지적을 반영할 때 더하거나 고친 시험 10개는 첫 구현 코드에서 모두 실패하는 것을 확인했다(이미 SECURITY인 행은 SANDBOX_PARTNERS 그대로, 분류 지정 409
CONFLICT, 문 수 319 대 39, 프록시와 썸네일을 한 문장으로 잠금, 커밋 실패 뒤에도 ASSET_CLASSIFIED 행이 남음, lowered에 revoked_grant_ids 없음).
