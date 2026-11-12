# ADR-0074: 분류를 더 엄격하게 바꾸면 공유 등급을 함께 내린다

- 상태: accepted
- 날짜: 2026-11-12
- 관련: 13번 요구사항 2.2(data_role CORE 변경의 하향)·3.6(가장 엄격한 값 상속)·4.6(운영자의 분류 지정)·10.3(하향과 기존 권한 처리), domain.md 3장(상속)·4장(공유 가능 조건, asset-metadata 불변식), ADR-0019(공유 판단 한 곳), ADR-0028(약정 종료는 공유를 자동으로 바꾸지 않음), ADR-0053, 계약 점검 S11

## 맥락

`PATCH /v1/assets/{asset_id}/classification`(운영자)은 sensitivity_class·privacy_status만 바꾸고 sharing_scope는 보지 않았다.
1. L2(SANDBOX_PARTNERS)로 공유 중인 비식별본을 SECURITY로 지정하면 200을 돌려준 뒤에도 L2에 남았다. `GET /v1/assets/{asset_id}` 응답이
   계약 asset-metadata의 'L2 이상 등급의 불변식'(sensitivity_class ∈ {INTERNAL, RESTRICTED})을 어겼다.
2. 원본을 SECURITY로 지정하면 파생본은 원 자산의 가장 엄격한 민감도를 물려받아(domain.md 3장) 실효 민감도가 SECURITY인데, 등급은 L2 그대로였다.
3. 데이터셋 버전 등급과 이용 권한 판단은 저장된 sharing_scope를 본다. 그래서 운영자가 방금 보안 민감으로 지정한 데이터가 기존 이용기관에 계속 나갔다.
   공유 조건(Sharing_Rules)은 등급을 올릴 때만 평가했다. 처리 함수의 설명("더 엄격하게 바꾸면 파생본 등급은 규칙으로 다시 본다")과도 달랐다.

불변식은 지켜야 한다. 다만 어떻게 지킬지(시스템이 내릴지, 거부할지, 기존 권한을 어떻게 할지)는 정해진 것이 없었다.

## 결정

1. **분류를 바꾼 뒤 그 자산과 모든 파생본(계보 아래쪽)의 등급을 다시 본다.** 판단은 Sharing_Rules의 새 순수 함수
   `scope_after_change(current, before, after)` 한 곳에서 한다. 바꾸기 전 사실로도 지금 등급에서 충족하지 못하던 조건은 그대로 두고,
   이번 변경으로 새로 충족하지 못하게 된 조건이 없는 가장 높은 등급(지금 등급 이하)을 돌려준다.
   예: L2 이상에서 SECURITY → L1, L4에서 RESTRICTED → L3(데이터셋 버전에 들어 있으면) 또는 L2.
2. **지금 등급보다 낮으면 시스템이 바로 내린다**(`sharing.downgrade`). 기존 이용 권한은 **즉시 철회(REVOKE_NOW)**다 — 그 자산이 든
   데이터셋 버전의 GRANTED 권한을 끊고 이용기관에 알린다. data_role을 CORE로 바꿀 때(요구사항 2.2, `PATCH /v1/assets/{asset_id}`)와 같은 처리다.
3. 내린 자산은 `ASSET_CLASSIFIED` 감사의 `details.lowered`(자산 ID, 전·후 등급)와 `details.existing_grant_handling`에 남긴다. 응답 본문과
   상태 코드는 바꾸지 않는다.
4. 분류를 바꾸기 전에 그 자산과 파생본 행을 잠그고 다시 읽는다(원본 먼저, 파생본은 ID 순). 같은 때 들어온 등급 변경 신청이 바뀌기 전
   분류로 올리고, 이쪽은 올리기 전 등급을 보고 지나치는 일을 막는다.
5. 계약 `classifyAsset`의 설명에 이 부수 효과를 적었다(계약 먼저).

버린 대안:
- **422 RULE_VIOLATION으로 거부하고 ORG_ADMIN이 먼저 내리게 하기** — 불변식은 지키지만, 운영자가 보안 민감 사실을 기록하지 못하고 그동안
  데이터는 계속 공유된다. 분류 지정은 운영자(`asset:classify`), 하향 신청은 ORG_ADMIN의 일이라 서로를 기다리게 된다.
- **지금 사실로 `max_scope_for`를 다시 계산해 그보다 높으면 내리기** — 분류와 상관없는 이유(2단 약정 만료 등)로도 등급을 내리고 권한을 끊는다.
  약정 종료는 공유·이용 권한을 자동으로 거두지 않기로 했다(ADR-0028). 분류 지정이 그 결정을 우회해서는 안 된다.
- **기존 권한을 만료까지 유지(KEEP_UNTIL_EXPIRY)** — 보안 민감으로 지정된 데이터가 권한 기간 내내 나간다. 요청 본문으로 처리 방식을 받는 것은
  계약에 새 필드가 생기는 일이라 이번에는 하지 않았다(아래 감수할 점).

## 결과

좋아지는 점: 분류 지정 직후 저장된 상태가 domain.md 4장과 asset-metadata 불변식에 맞는다. 보안 민감 데이터의 이용 권한이 바로 끊긴다.
다시 올리려는 신청도 바뀐 분류로 막힌다(기존 규칙).

감수할 점: 운영자는 권한 처리 방식을 고를 수 없다 — 항상 즉시 철회다. L4 → L3처럼 이용기관 공유가 계속 되는 하향에서도 그 자산이 든 버전의
권한이 끊긴다(기존 하향 처리 `on_downgrade`가 버전 단위로 끊는다). domain.md는 하향 때 '이미 부여된 이용 권한의 처리(즉시 철회 / 만료까지
유지)를 함께 정한다'고만 하므로, 기본값과 선택지를 둘지는 소유자가 정할 일로 남긴다. 하향은 별도 감사 행이 아니라 `ASSET_CLASSIFIED`의
details에 남는다. 분류 말고 다른 사실이 바뀌는 경로(검수 결과 변경 등)는 이번에 손대지 않았다.

되돌리는 방법: `classify_asset`의 잠금·`lower_after_classification` 호출·감사 details 두 항목, `sharing.lower_after_classification`,
`sharing_rules.scope_after_change`를 지우고 계약 `classifyAsset`의 description을 지운 뒤 웹·Go 타입을 다시 만든다. 마이그레이션 없음.

시험: `tests/integration/test_classification_lowers_scope.py` 3개(비식별본 SECURITY → L1·권한 철회·불변식 계약 검사, 원본 SECURITY →
파생본 L1, 약정 만료만으로는 내리지 않음), `tests/unit/test_scope_after_change.py` 4개(속성 기반 시험 포함). 수정 전 코드에서 통합 시험
3개가 모두 실패하고(등급이 SANDBOX_PARTNERS 그대로) 단위 시험은 함수가 없어 모이지 않는 것을 확인했다.
