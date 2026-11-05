# ADR-0028: 약정·연계 카드 등록 API — 약정 종료는 유효기간으로, 카드는 분야별 자유 기록 + 정책 코드만 검사

- 상태: accepted (실제 연계 카드 양식 확보 후 integration-card 스키마 갱신 예정)
- 날짜: 2026-11-05
- 관련: 03번 요구사항 1.4·1.5, ONB-01, domain.md 1장(기관 약정)·9장(local_copy_policy), 기준 문서 부록 A, AGENTS.md 4·5번

## 맥락
약정은 지금까지 관리 명령(`python -m app.cli agreement`)으로만 기록했고, 연계 카드는 표(0002)만 있고 등록 수단이 없었다.
13번 공유 규칙은 2단 약정 유효 여부를 `agreements`에서 읽고, 08번 백업·장비 화면은 장비의 `local_copy_policy`를 읽는다.
연계 카드의 실제 양식은 아직 없다 — 기준 문서 부록 A에는 분야와 질문, 가정 예시만 있다.

## 결정
1. **약정 API는 운영자만, 다중 인증**: `POST /v1/agreements`(TIER1은 허용 목적 없음, TIER2는 하나 이상, 목적은 purpose 코드표),
   조회는 운영자 전체·제공기관 자기 기관. 약정서 원본은 저장하지 않고 참조만 기록한다.
2. **약정 종료는 새 상태 코드 없이 `valid_until`을 당긴다** (`POST /v1/agreements/{id}/end`, 사유 필수·감사). domain.md에 약정 상태
   코드표가 없으므로 코드값을 늘리지 않는다. 13번 `tier2_valid()`가 유효기간으로 판단하므로 종료가 바로 반영된다.
   이미 공유된 자산·부여된 이용 권한은 자동으로 바꾸지 않는다 — 13번 하향·철회 절차(사람 판단)를 따른다.
3. **연계 카드 내용은 `contracts/schemas/integration-card.schema.json`**: 부록 A의 15개 분야를 객체로 두고 분야 안은 자유 기록
   (`additionalProperties: true`). 정책을 움직이는 값만 domain.md 코드로 검사한다: `collection_mode_requested`,
   `current_storage.local_copy_policy`. 담당자는 이름·연락처 대신 플랫폼 사용자 ID와 역할만(개인정보 최소화).
4. **카드 → 장비**: 카드에 장비를 지정하면 `local_copy_policy`를 그 장비 기록에 반영하고 따로 감사(`DEVICE_LOCAL_COPY_POLICY_CHANGED`).
   희망 `collection_mode`는 기록만 한다 — 장비 정책은 운영기관이 정한다. 정책 연결은 `set-policy --integration-card`
   (같은 기관 카드만)로 `device_policies.integration_card_id`에 남기고 카드 응답 `linked_policies`로 보인다.
5. 카드는 ORG_ADMIN이 자기 기관으로만 쓴다. 버전은 바꾸지 않고 새 버전으로만(`/versions`).

## 결과
- 좋아지는 점: 09번 온보딩 화면과 13번 약정 관리의 선행 API가 생겼다. 공유 규칙·백업 안내가 같은 기록을 쓴다.
- 감수할 점: 카드 분야 안이 자유 기록이라 비교·집계가 어렵다 — 실제 카드 양식을 받으면 분야별 필드를 좁힌다(스키마 부버전).
  약정 종료가 공유·이용 권한을 자동으로 거두지 않는다(의도).
- 되돌리는 방법: 새 경로·권한·스키마 제거. DB 구조 변경 없음(0002 표 사용).
