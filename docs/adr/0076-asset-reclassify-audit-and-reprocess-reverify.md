# ADR-0076: 자산 수정의 감사 시점·하향 감사, 검증 실패 자산의 재처리

- 상태: accepted
- 날짜: 2026-11-12
- 관련: 05번 요구사항 7.2(재처리), 13번 요구사항 2.1·2.2(data_role 변경)·10.3(하향), 04번 요구사항 5.3(회차 지정),
  ADR-0053(응답 전 커밋, 감사 트랜잭션), ADR-0058(검증 이어 하기), security.md 감사 로그, 계약 점검 S22~S26

## 맥락

계약 점검에서 `PATCH /v1/assets/{asset_id}`와 `POST /v1/assets/{asset_id}/reprocess`의 빈틈 다섯 가지가 나왔다.

1. **S22 재처리**: 무결성 검증이 재시도를 다 쓰면(저장소 장애 등) 자산은 VALIDATING → FAILED(failed_from=VALIDATING)가 된다.
   재처리는 상태만 VALIDATING으로 돌리고 요청한 처리 단계(메타 추출 등)만 넣었다. 처리 단계는 REGISTERED가 아니면 건너뛰고
   건너뛴 단계는 다음 단계를 잇지 않는다. 검증 작업은 다시 들어가지 않아 자산이 VALIDATING에 영구히 남았다
   (다음 재처리 409, 삭제도 VALIDATING이라 거부, 업로드 예약도 풀리지 않음). 계약은 "실패 직전 단계부터 다시 시작한다"이다.
2. **S23 data_role 변경 감사**: `DATA_ROLE_CHANGED`를 연결 없이 써서 바로 따로 커밋됐다. 같은 요청의 회차 지정이 404·409·422로
   실패해 변경이 되돌아가도 SUCCESS 감사가 남아, 일어나지 않은 변경을 기록했다.
3. **S24 권한 거부 감사**: ORG_MEMBER는 `asset:write`로 들어와 경로 안에서 403을 받는데 `PERMISSION_DENIED`를 남기지 않았다.
4. **S25 하향 감사**: CORE로 바꾸면 파생본의 공유 등급을 PROGRAM_REVIEW로 내리고(13번 요구사항 2.2) 그 파생본이 든 버전의
   이용 권한을 즉시 철회한다. 감사는 원본의 `DATA_ROLE_CHANGED` 한 줄뿐이라 어떤 자산의 등급이 어디서 내려갔는지,
   어떤 권한이 끊겼는지 남지 않았다 (security.md: 공유 등급 변경·이용 철회는 반드시 기록, domain.md: 하향도 사유와 함께).
5. **S26 계약**: 다른 실증사업의 회차를 지정하면 422 RULE_VIOLATION인데 계약 응답 목록에 422가 없었다.

## 결정

1. **재처리(S22)**: 실패 직전 단계가 VALIDATING(또는 RECEIVED)이면 상태를 돌린 뒤 **무결성 검증 작업**을 넣는다.
   검증은 VALIDATING 자산을 이어 하고(ADR-0058, 격리 사유가 남아 있으면 재검증) 예약 정산·무결성 로그를 한 번만 한다.
   등록되면 지금처럼 `schedule_after_register`가 보통 처리 순서(메타 추출 → 프록시 → 썸네일 …)를 넣으므로
   **요청한 단계는 따로 넣지 않는다** — 등록 전에는 처리 결과가 하나도 없어 처음부터 도는 것이 '직전 단계부터'와 같다.
   끝난(FAILED) 검증 작업이 원래 중복 키를 쥐고 있으므로 이번 재처리 실행의 키(`integrity_verify:{asset}:{run}`)를 쓴다.
   작업 행은 요청 트랜잭션에 넣는다(요청이 되돌아가면 작업도 없다). 응답의 job_ids는 이 검증 작업이다 — 계약 설명에 적었다.
   **재처리는 상태를 보기 전에 자산 행을 잠그고 다시 읽는다**(`db.refresh(a, with_for_update=True)`, 구간 클립 요청과 같은
   방식). '한 번만'은 이 잠금을 전제로 한다. 잠그지 않고 읽은 값으로 판단하면, 늦은 요청이 FAILED를 읽은 뒤 앞선 재처리와
   그 검증이 끝나 등록된 자산을 VALIDATING으로 덮어쓰고 검증을 또 넣는다. 그 검증은 등록된 자산의 무결성 로그를 다시 쓰려다
   고유키(`uq_integrity_log_asset_id`)에 걸려 재시도를 다 쓰고 FAILED(failed_from=VALIDATING)가 되며, 그 뒤 재처리할 때마다
   같은 일이 되풀이돼 정상 등록된 원본이 갇힌다. 잠그면 늦은 요청은 최신 상태를 본다: 등록됐으면 보통 재처리, 검증 중이면 409다.
   검증의 두 트랜잭션도 자산 행을 잠그므로 진행 중인 검증과도 겹치지 않는다. 잠금 순서는 삭제와 같은 '원 자산 → 파생본'이다.
2. **data_role 변경 감사(S23)**: 회차 지정까지 모든 검사와 마지막 flush를 통과한 뒤 `connection=db.connection()`으로
   요청 트랜잭션에 쓴다 (운영 정책 PATCH와 같은 방식). {from, to}는 바꾸기 전 값을 잡아 둔다.
3. **권한 거부 감사(S24)**: 403 직전에 `PERMISSION_DENIED`(result DENIED, 대상 자산, details
   `{action: "asset:data_role", from, to}`)를 **따로 커밋**한다 — 요청이 되돌아가도 남아야 한다(require()의 거부와 같다).
   지금 값과 같은 data_role은 변경이 아니라 거부도 감사도 없다(전과 같다). 역할 검사가 사유 검사보다 먼저인 순서도 그대로다.
4. **하향 감사(S25)**: 등급을 내린 자산마다 새 감사 동작 **`SHARE_SCOPE_DOWNGRADED`**(대상 그 자산, 사유는 요청 사유,
   details `{from, to, existing_grant_handling: REVOKE_NOW, cause: DATA_ROLE_CHANGED, origin_asset_id, revoked_grant_ids}`)를,
   끊긴 이용 권한마다 수동 철회와 같은 **`GRANT_REVOKED`**(대상 Grant, 기관은 이용 기관, details `{cause, asset_id}`)를 남긴다.
   모두 2번과 같은 자리·같은 트랜잭션이다. 이를 위해 `sharing.downgrade`가 끊은 권한 ID를 돌려준다(전에는 버렸다).
5. **계약(S26)**: patchAsset에 `'422': Unprocessable`을 더하고, 회차가 자산과 같은 실증사업이어야 한다는 조건을 설명에 적었다.
   구현은 그대로다.

버린 대안:
- S22에서 failed_from이 VALIDATING이면 409로 거부하고 계약에 적기 — FAILED에서 나갈 API 경로가 없어지고
  domain.md 5장 "FAILED → 실패 직전 단계(PLATFORM_OPERATOR, 재처리)"와 어긋난다.
- S22에서 검증 작업에 요청한 단계를 사슬로 달고 검증 뒤 이어 넣기 — 등록 때 `schedule_after_register`도 보통 순서를 넣으므로
  같은 단계가 두 번 돈다. 막으려면 등록 경로를 바꿔야 해 범위가 커진다.
- S23에서 감사를 연결 없이 검사 뒤로만 옮기기 — 검사 실패는 막지만 flush 이후의 DB 오류·커밋 실패에는 여전히 남는다.
  요청 트랜잭션에 쓰면 감사 해시 체인 잠금을 커밋까지 쥐지만, 응답 직전에 쓰므로 그 시간은 짧다.
- S25에서 기존 `SHARE_SCOPE_REQUESTED`를 다시 쓰기 — 공유 등급 신청(ShareRequest)이 없는데 신청으로 읽힌다.
  감사 동작 이름은 domain.md·계약에 목록이 없어 새 이름을 더해도 그 문서를 바꾸지 않는다.
- S25에서 하향 경로마다 ShareRequest 행을 만들기 — 공유 등급 신청 목록 API에 보이는 데이터가 바뀐다. 이번에는 감사만 더한다.

## 결과

좋아지는 점: 저장소 장애로 검증이 실패한 자산을 운영자가 재처리하면 등록·정산까지 끝난다. 감사 로그에 일어나지 않은
data_role 변경이 남지 않는다. ORG_MEMBER의 data_role 변경 시도가 남는다. CORE 변경으로 등급이 내려간 파생본과 끊긴
이용 권한을 그 자산·권한의 감사 이력에서 찾을 수 있다. 다른 실증사업 회차의 422가 계약에 있다.

감수할 점:
- 검증 실패 자산의 재처리는 요청한 단계와 상관없이 등록 뒤 보통 처리 순서 전체를 돈다. REPROCESS_REQUESTED 감사의
  steps는 요청 그대로다.
- 이 수정 전에 재처리로 이미 VALIDATING에 갇힌 자산은 자동으로 풀리지 않는다(작업이 없다). 운영 DB에 있으면 따로 확인해야 한다.
- 공유 심사의 공유 철회(WITHDRAW)와 공유 등급 신청의 하향도 `sharing.downgrade`로 권한을 끊지만 이번에는 바꾸지 않았다 —
  끊긴 권한마다의 `GRANT_REVOKED`는 아직 이 경로에만 있다.
- 회차 지정이 실패해 되돌아간 CORE 변경 요청에서도 이용 기관 철회 알림(메일)은 이미 보냈을 수 있다(알림은 트랜잭션 밖이다).
  감사·DB는 되돌아간다.

되돌리는 방법: `reprocess_asset`의 검증 작업 분기와 자산 행 잠금, `patch_asset`의 `PERMISSION_DENIED`·`_audit_reclassified`,
`sharing.downgrade`의 반환값을 되돌리고 계약의 patchAsset 422·설명과 reprocessAsset 설명을 되돌린 뒤 웹·에이전트 타입을
다시 만든다. 마이그레이션 없음.

시험: `tests/integration/test_asset_patch_audit.py` 8개 — 회차 지정 실패 세 가지(422·404·409)에서 DATA_ROLE_CHANGED 없음,
성공한 변경의 감사 한 줄(대조), ORG_MEMBER 403의 PERMISSION_DENIED, CORE 변경의 SHARE_SCOPE_DOWNGRADED·GRANT_REVOKED,
실패한 CORE 변경은 등급·권한·감사가 그대로, 다른 실증사업 회차 422의 계약 일치. `tests/integration/test_verify_resume.py::
test_reprocess_after_exhausted_verification_restarts_verification` — 실제 워커로 검증 재시도를 다 쓴 뒤 재처리하면 등록·
예약 정산·무결성 로그 한 번, 보통 처리 순서 한 번. `test_verify_resume.py::
test_overlapping_reprocess_decides_on_the_locked_current_state` 2개 — 늦은 재처리 요청이 FAILED를 읽은 뒤 앞선 재처리가
확정되면, 그 검증까지 끝나 등록된 자산은 보통 재처리(202, 처리 단계 작업)가 되고 상태·무결성 로그·예약이 그대로이며,
검증이 아직이면 409이고 검증 작업은 앞선 하나뿐이다. 수정 전 코드에서 새 시험 9개 중 대조 시험(성공한 변경의 감사 한 줄)을
뺀 8개가 실패하는 것을 확인했다. 겹친 재처리 시험 2개는 잠금 없이(검토 지적 전 이 브랜치) 실패하는 것을 확인했다 —
등록된 자산이 `(202, 'VALIDATING')`, 검증 중인 자산이 409가 아니라 202였다.

## 이후 보완 (통합 검토, 2026-11-20)

- **CORE 변경의 잠금 (통합 검토 ASSETS-1·ASSETS-3)**: `patch_asset`은 CORE로 바꾸는 요청이면 판단하기 전에
  `sharing.lock_family`로 자신과 모든 파생본을 원 자산 → 파생본 순으로 잠그고 다시 읽는다(ADR-0074 분류 지정과 같은 순서).
  전에는 잠그지 않고 읽은 파생본의 등급을 내리면서 그 행부터 잠그고(하향 뒤 첫 조회의 autoflush) 원 자산은 마지막 flush에서
  잠갔다. 그래서 원 자산 → 파생본 순으로 잠그는 분류 지정·삭제·검수 결정과 교착해 한쪽이 409 CONFLICT를 받았다. 또 그사이
  확정된 파생본의 L1 → L2 상향(그 신청은 원 자산의 바꾸기 전 BYPRODUCT를 본다)을 보지 못해 CORE 원본의 파생본이 L2에 남았다
  (domain.md 3장 위반). 이제 겹친 상향 신청은 이 잠금을 기다린 뒤 CORE를 보고 422가 되고, 먼저 확정됐으면 CORE 변경이 다시
  읽은 값으로 그 파생본을 내린다. CORE로 바꾸는 요청은 ORG_ADMIN 검사보다 먼저 잠근다 — 잠근 뒤의 값으로 '바뀌는가'를
  판단하기 위해서다. 거부되면 요청 트랜잭션이 되돌아가며 바로 놓는다. 잠그는 행은 분류 지정과 같다(등급과 상관없이 모든 파생본).
  시험 `tests/integration/test_core_change_family_locks.py` 2개는 수정 전 코드에서 실패했다 — CORE 변경이 파생본 행을 쥔 채
  원 자산을 기다렸고, 겹친 상향 뒤 끝 상태가 `('CORE', 'SANDBOX_PARTNERS')`였다.
- **재처리와 겹친 삭제 (통합 검토 ASSETS-4)**: 재처리는 행을 잠그고 다시 읽은 뒤 `deleted_at`부터 본다. 잠그기 전에 삭제가
  확정됐으면(삭제도 같은 행을 잠근다) `load_scoped`와 같은 404 NOT_FOUND다. 전에는 다시 읽은 행의 삭제 표시를 보지 않아 지워진
  자산에 202·처리 작업·REPROCESS_REQUESTED SUCCESS 감사가 남고, 지워진 자산과 파생본에 재처리 시각이 찍혔다. 404는 계약
  reprocessAsset에 이미 있는 응답이다. 시험 `tests/integration/test_reprocess_deleted_asset.py`는 수정 전 코드에서 202로 실패했다.
