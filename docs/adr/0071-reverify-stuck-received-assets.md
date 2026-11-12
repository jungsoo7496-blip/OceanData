# ADR-0071: RECEIVED에 멈춘 자산은 운영자 명령으로 검증 작업을 다시 넣는다 (상태 전이는 더하지 않는다)

- 상태: accepted
- 날짜: 2026-11-12
- 관련: 04번 요구사항 4.1·4.7·1.5, ADR-0031(악성코드 검사기 포트), ADR-0046(검증 중 삭제 거부), ADR-0058(검증 이어 하기·한 번만 등록),
  ADR-0059(건마다 짧은 트랜잭션), ADR-0063(사유 문자 검사), domain.md 5장(ingest_status), 검토 2026-11-12 C2·'사람이 정할 것' 2,
  계약 점검 2026-11-05 발견 1(끝난 작업이 dedupe 키를 쥔다)

## 맥락

ADR-0058은 `VALIDATING`에 멈추는 경우를 고쳤고, `RECEIVED`에 멈추는 경우는 상태표 문제라 남겼다. 지금 코드가 하는 일은 이렇다.

1. 기관 정책이 악성코드 검사를 요구하는데(`organizations.malware_scan_required`) 워커의 `MALWARE_SCANNER`가 `none`이면,
   `IntegrityVerifier.verify`의 첫 트랜잭션이 `RECEIVED → VALIDATING` 전이 **전에** `ScannerUnavailable`을 던진다. 트랜잭션은 되돌아가 자산은 그대로다.
2. 작업 루프(`run_once`)는 이 예외를 알려진 오류로 보지 않아 오류 코드 `INTERNAL`로 기록하고 지수 백오프(15·30·60·120초)로 다시 시도한다.
   `job_policies`의 `integrity_verify`는 최대 5회라 약 4분 뒤 작업이 `FAILED`가 된다.
3. 재시도를 다 쓰면 `_asset_failed → pipeline.mark_failed(c, asset_id, "INTERNAL")`가 불린다. 자산 행을 잠근 뒤
   `states.can_transition("ingest_status", "RECEIVED", "FAILED", {"SERVICE"})`를 보는데, 코드의 표는 `VALIDATING·REGISTERED·PROXIED → FAILED`만 허용하므로
   **아무것도 바꾸지 않고 확정한다**: ingest_status는 `RECEIVED`, `ingest_context`에 `failed_from`·`error_code`도 남지 않고, 업로드 행의 `reserved_bytes`와
   기관 쿼터의 `reserved_bytes`도 그대로다. domain.md 5장 표('처리 단계 → FAILED')와 맞는 동작이라 버그로 보지 않고 바꾸지 않는다.
4. 처음 작업의 dedupe 키(`integrity_verify:{asset}:{upload}`)는 유일 색인이 끝난 작업에도 걸려 있어(계약 점검 발견 1) 같은 키로는 새 작업이 생기지 않는다.
   완료 요청을 다시 보내도 업로드가 이미 `RECEIVED`라 바로 돌아온다. 재처리 API는 `RECEIVED`를 409로, 삭제도 409로 거부한다(ADR-0046).
   검사기가 돌아와도 자산을 진행시킬 길이 DB 직접 조작뿐이었고, 그동안 예약이 기관 쿼터를 깎았다.
5. 이 상태를 알리는 감시 규칙은 없다. `PROCESSING_BACKLOG`는 `max_wait_s`(기본 3600초)보다 오래 기다린 **QUEUED** 작업만 센다 — 재시도 동안은 실행 시각이
   백오프만큼 미뤄지고, 다 쓰면 `FAILED`라 세지 않는다. 남는 신호는 워커 로그 `job_failed`, 지표 `worker_job_failures_total{job_type="integrity_verify",error_code="INTERNAL"}`,
   수집 상태(`GET /v1/ingest-status`)의 24시간 실패 원인 상위(`PROCESSING`/`INTERNAL`)뿐이고, 어느 것도 자산을 가리키거나 '멈췄다'고 말하지 않는다.

같은 끝 상태(대기·실행 중인 검증 작업 없이 `RECEIVED`)는 다른 원인으로도 생긴다: 스토리지 장애로 객체를 읽지 못해 재시도를 다 쓴 경우, 그리고 코드로 보면
완료 요청의 작업 등록은 따로 커밋되므로(`_mark_received`) 요청 커밋이 실패한 뒤 재시도 전에 워커가 그 작업을 돌리면 작업은 `DONE`(자산이 아직 UPLOADING)으로 끝나고,
재시도의 등록은 같은 키의 `DONE` 작업을 돌려받는다. 마지막 경우는 이번에 재현하지 않았다(아래 '감수할 점').

## 결정

1. **상태 전이를 더하지 않는다.** `RECEIVED → FAILED(예약 해제)`는 domain.md 5장 변경이라 사람 결정으로 남긴다(AGENTS.md 4). 검토가 대안으로 든 운영자 재투입 경로만 둔다:
   `python -m app.cli requeue-verify (--asset <asset_id> | --all-stuck) [--org <organization_id>] [--dry-run] [--reason <사유>]` (`app/services/verify_requeue.py`).
2. **대상은 '대기·실행 중인 검증 작업이 없는 RECEIVED 자산'이다** (삭제 표시 없음). 마지막 검증 작업이 `FAILED`·`DONE`이거나 작업이 아예 없는 자산이다.
   재시도 대기(실행 시각이 미래인 `QUEUED`)와 임대 중인 `RUNNING`은 진행 중으로 보고 건드리지 않는다 — 스스로 끝나거나 임대 회수가 되돌린다.
   다른 상태의 자산도 건드리지 않는다. `--dry-run`은 아무것도 바꾸지 않고 자산마다 기관·`updated_at`·묶인 예약(`reserved_bytes`)·검사 요구 여부·마지막 검증 작업
   (상태·시도 수·오류 코드)을 JSON으로 보인다. `--asset`으로 고른 자산이 대상이 아니면 까닭(`VERIFY_JOB_ACTIVE`·`NOT_RECEIVED`)을 보이고 아무것도 하지 않는다.
3. **자산마다 자기 트랜잭션에서 잠그고 다시 확인한 뒤 넣는다** (ADR-0059와 같은 방식). 후보는 잠그지 않고 고르고, 자산 행을 `FOR UPDATE SKIP LOCKED`로 잠근다 —
   워커 검증의 첫 트랜잭션(`RECEIVED → VALIDATING`)과 같은 행 잠금이라 둘이 겹치지 않고, 다른 실행·요청이 잡고 있으면 기다리지 않고 `LOCKED`로 건너뛴다.
   잠근 뒤 상태와 진행 중인 작업을 다시 본다. 새 작업의 dedupe 키는 `integrity_verify:{asset}:requeue`에 `dedupe_active_only`다 — 대기·실행 중이면 막고, 끝난 재투입 작업은
   키를 놓아 다음 재투입을 막지 않는다(DB 유일 색인이 두 번째 방어다). 작업과 감사는 같은 트랜잭션(`connection=`)으로 함께 확정·취소한다.
   payload는 `{asset_id, upload_id, requeued: true}`이고 **자산 행은 바꾸지 않는다**. 한 자산이 실패하면 기록하고 다음 자산으로 간다(종료 코드 1).
4. **등록·예약 정산은 기존 검증이 한다.** 검증의 두 번째 트랜잭션이 자산 행을 잠그고 `VALIDATING`인지 다시 보므로(ADR-0058) 겹쳐 돌아도 등록·무결성 로그·정산은 한 번이다.
5. **감사는 자산마다 `INTEGRITY_VERIFY_REQUEUED`**: 주체 `SYSTEM`/`cli`(다른 운영자 CLI 명령과 같다), 대상 `Asset`, 사유, `details`에 새 작업 ID·앞 작업(상태·시도·오류)·묶인 예약.
   다른 CLI 명령은 사유를 받지 않지만 같은 성격의 API(재처리, `REPROCESS_REQUESTED`)가 사유를 요구해 맞췄다 — 실행할 때 `--reason`이 필수이고(2~500자),
   감사에 남는 글이라 API 사유와 같은 문자 검사(`CLIP_REASON_PATTERN`, ADR-0063)를 한다. `--dry-run`은 감사를 남기지 않는다.
6. **탐지는 목록과 Runbook으로.** 감시 규칙 코드는 도메인 데이터라 새로 만들지 않는다. `--all-stuck --dry-run`이 목록을 보이고,
   `docs/runbook/11-verify-stuck-received.md`에 신호·확인·재투입·완료 확인 절차를 적었다. 명령이 본 `MALWARE_SCANNER`가 `none`인데 검사가 필요한 기관의 자산이 있으면
   표준 오류로 경고한다 — 명령(`migrate` 컨테이너)과 워커의 환경이 다를 수 있어 거부하지는 않는다.
7. `_asset_failed`·`pipeline.mark_failed`는 바꾸지 않는다(맥락 3).

버린 대안:
- `RECEIVED → FAILED(예약 해제)` 전이 추가 — 상태표 변경이라 사람 결정이다(ADR-0058 '감수할 점').
- 처음 작업을 `QUEUED`로 되돌리고 시도 수를 0으로 — 작업 기록(시도·오류)을 지우고 큐 내부 상태를 직접 고친다. 새 작업을 넣으면 앞 기록이 그대로 남는다.
- 처음 작업과 같은 키를 `dedupe_active_only`로 다시 쓰기 — 처음 작업의 키를 바꿔 놓게 된다. 처음 작업 기록은 그대로 두고 재투입 작업을 구분하려고 키를 따로 둔다.
- 워커가 주기적으로 스스로 다시 넣기 — 검사기 부재가 길면 몇 분마다 실패를 되풀이해 로그·지표를 채운다. 원인을 없앤 뒤 언제 다시 넣을지는 운영자가 정한다.
- 잠금을 기다리기(`SKIP LOCKED` 없이) — 검증 중인 자산이나 긴 요청 뒤에서 명령이 멈춘다. 건너뛰고 다시 돌리게 한다.
- 감시 규칙(예: 'RECEIVED 장기 체류') 추가 — 규칙 코드는 domain 데이터라 사람 결정으로 제안만 한다.

## 결과

좋아지는 점: 검사기(또는 스토리지)가 돌아온 뒤 DB를 만지지 않고 멈춘 자산을 등록까지 보낼 수 있고, 묶인 예약이 한 번만 정산된다. 운영자가 바꾸기 전에 대상을 본다.
여러 번·동시에 돌려도 자산마다 대기·실행 중인 검증 작업은 하나이고, 넣은 것마다 사유와 함께 감사가 남는다. 정상 진행 중인 자산과 다른 상태의 자산은 건드리지 않는다.

감수할 점·남은 일:
- **자동으로 드러나지 않는다.** 운영자가 dry-run을 돌리거나 실패 신호를 보고 찾아야 한다. 감시 규칙을 둘지는 사람 결정이다.
- **검사기가 계속 없으면 예약은 계속 묶인다.** `RECEIVED → FAILED(예약 해제)` 전이를 둘지는 여전히 사람 결정이다(검토 '사람이 정할 것' 2). 원인이 남은 채 다시 넣으면 다시 5회 실패한다(해는 없다).
- 실제 검사기 제품은 결정 대기다(ADR-0031). 지금 어댑터는 `none`과 시험용 `mock-eicar`뿐이라, 운영에서 '검사기가 돌아온다'는 제품 어댑터가 생긴 뒤의 일이다(**확인 필요**).
- 명령은 자기 환경의 `MALWARE_SCANNER`만 안다. 워커를 다시 띄웠는지는 따로 확인한다(Runbook 11).
- 감사 기록이 자산 행 잠금을 쥔 채 전역 감사 잠금을 기다린다. 자산 하나씩 짧게 확정하므로 영향은 작다. 교착이 나면 그 자산만 실패로 보고된다.
- 완료 요청의 작업 등록이 요청 트랜잭션과 따로 커밋되는 틈(맥락 끝 문단)은 코드로 본 것이고 재현·수정하지 않았다. 생기면 이 명령으로 복구된다.

되돌리는 방법: `app/services/verify_requeue.py`와 `app/cli.py`의 `requeue-verify`(파서·`_requeue_verify`)를 지우고 Runbook 11과 색인 줄, 07번의 안내 줄을 지운다.
이미 넣은 재투입 작업은 보통 작업처럼 끝나고 감사 행은 남는다. 마이그레이션·계약 변경 없음.

## 시험

`tests/integration/test_verify_requeue.py` 3개 (실제 S3 API `ictx`, 합성 CSV):
- 검사기 없이 5회 실패 → 작업 `FAILED`, 자산 `RECEIVED`·예약 유지·`mark_failed`가 아무것도 바꾸지 않음 → dry-run 목록(앞 작업 `FAILED`/5회/`INTERNAL`, 예약, 검사 요구) → 사유 없이는 거부 →
  검사기 없이 다시 넣으면 새 작업과 경고 → 두 번 돌려도 일을 더 만들지 않음 → 또 실패한 뒤 검사기가 돌아오면 다시 넣기(끝난 재투입 작업이 키를 쥐지 않음) → 워커 검증 →
  `READY_PRIVATE`·무결성 로그 1건·예약 한 번 정산 → 그 뒤에는 대상이 아님 → 감사 2건(주체·사유·새 작업·앞 작업).
- 정상 처리된 자산·`RUNNING`·`QUEUED`(재시도 대기 포함) 자산은 그대로이고 작업이 `DONE`인 `RECEIVED` 자산과 작업이 없는 자산만 대상, `--org` 범위, 기존 작업·자산 행은 하나도 바뀌지 않음,
  `--asset`으로 고른 진행 중·다른 상태 자산은 까닭만 보이고, 없는 자산·다른 기관으로 한정한 자산은 종료 코드 1.
- 동시 실행: 앞선 실행이 자산 행을 쥔 동안 뒤 실행은 `LOCKED`로 건너뛰고, 그다음 실행은 `VERIFY_JOB_ACTIVE` — 작업·감사 하나.

수정 전 코드에서 3개 모두 실패하는 것을 확인했다(명령이 없어 `argument cmd: invalid choice: 'requeue-verify'`로 `SystemExit: 2`, 모듈이 없어 `ImportError`).
첫 시험의 앞부분(5회 실패 뒤 `RECEIVED`·예약 유지·`mark_failed` 무변화)은 수정 전 코드에서 그대로 통과해 맥락 1~3을 확인한다.
재투입 키에서 `dedupe_active_only`를 빼면 첫 시험이 끝난 재투입 작업 ID를 돌려받아 실패하는 것도 확인했다.
