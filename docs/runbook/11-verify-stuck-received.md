# 11 검증 작업이 끝났는데 자산이 RECEIVED에 남음

업로드는 완료됐는데(ingest_status `RECEIVED`) 무결성 검증 작업이 재시도를 다 쓰고 FAILED로 끝나, 자산이 등록되지 않고
업로드 예약(`reserved_bytes`)도 풀리지 않은 상태다. 대표 원인은 **기관 정책이 악성코드 검사를 요구하는데(`malware_scan_required`)
워커의 검사기가 없는(`MALWARE_SCANNER=none`) 기간**이다 — 검증은 상태를 바꾸기 전에 멈추고 5회(약 4분) 다시 시도한 뒤 FAILED가 된다.
상태를 바꾸기 전에 나는 다른 오류(활성 품질 규칙 오류 `QualityRuleError`, 알 수 없는 `MALWARE_SCANNER` 값)도 같다. `RECEIVED → FAILED`
전이는 domain.md 5장 표에 없어 자산은 `RECEIVED`에 그대로 남는다(ADR-0071). 이 상태의 자산은 삭제도 409다(검증 중 삭제 거부, ADR-0046).

**이 문서의 대상이 아닌 것:** 스토리지 장애로 객체를 읽지 못해 재시도를 다 쓴 자산은 검증이 먼저 `VALIDATING`으로 옮긴 뒤라
`RECEIVED`에 남지 않고 `FAILED`(`ingest_context.failed_from=VALIDATING`)가 된다. 아래 명령의 목록에 나오지 않는다. 이 자산의 예약도
풀리지 않는데, 되돌리는 절차는 아직 없다(ADR-0071 감수할 점) — 스토리지는 [06-storage-failure.md](06-storage-failure.md)로 복구하고
해당 자산 ID를 수행 기록에 남긴다.

**판단 기준:** 이 상황만 따로 알리는 감시 규칙은 없다(PROCESSING_BACKLOG는 대기 중인 작업만 센다). 다음 신호가 보이면 확인한다.
- 워커 로그 `job_failed`(`job_type=integrity_verify`), 메트릭 `worker_job_failures_total{job_type="integrity_verify"}` 증가.
- 수집 상태(`GET /v1/ingest-status`) 실패 원인 상위에 `PROCESSING` / `INTERNAL`.
- 기관이 "업로드는 끝났는데 등록·영수증이 안 나온다", "쿼터가 줄었다"고 알린다.
- 정기 점검: 아래 1번 dry-run을 돌려 목록이 비어 있는지 본다.

**자동 대응:** 없다. 작업 큐는 최대 시도 뒤 다시 시도하지 않고, 처음 작업의 중복 방지 키가 남아 있어 같은 업로드로는 새 검증
작업이 생기지 않는다. 자산·예약은 바뀌지 않은 채 기다린다(데이터 손실은 없다 — 객체는 저장소에 있다).

**사람이 할 것**
1. 대상 확인 (아무것도 바꾸지 않는다):
   `$COMPOSE run --rm migrate python -m app.cli requeue-verify --all-stuck --dry-run` (기관 하나만: `--org <organization_id>`).
   `stuck`의 각 항목은 대기·실행 중인 검증 작업이 없는 `RECEIVED` 자산이다. `last_job`(마지막 검증 작업의 상태·시도 수·
   오류 코드, 없으면 null), `reserved_bytes`(묶인 예약), `malware_scan_required`, `updated_at`(오래된 것부터)을 본다.
   재시도 대기 중이거나 워커가 처리 중인 자산은 목록에 나오지 않는다 — 기다리면 스스로 끝난다.
2. 원인부터 없앤다.
   - 검사기: 워커에 검사기를 켠다(`.env`의 `MALWARE_SCANNER`를 바꾸고 `$COMPOSE up -d worker`. 클립 전용 워커
     `worker-clips`는 검증 작업을 가져가지 않아 다시 만들지 않아도 된다). 지금 저장소의 어댑터는
     `none`과 시험용 `mock-eicar`(실제 악성코드는 못 찾는다)뿐이다 — 실제 검사기 제품은 결정 대기(ADR-0031, **확인 필요**).
     기관 정책을 끄는 것(`python -m app.cli org-policy <기관> --malware-scan off`, 감사 ORG_POLICY_CHANGED)은 정책 결정이므로
     운영기관·해당 기관과 합의한 뒤에만 한다.
   - 품질 규칙 오류: 워커 로그 `job_failed`의 예외가 `QualityRuleError`면 활성 규칙을 바로잡는다(03번 품질 규칙).
     워커가 멈췄으면 [07-queue-stopped.md](07-queue-stopped.md).
3. 다시 넣기 (사유 필수, 자산마다 감사 `INTEGRITY_VERIFY_REQUEUED`):
   `$COMPOSE run --rm migrate python -m app.cli requeue-verify --all-stuck --reason "검사기 복구 뒤 재투입 (점검 기록 번호)"`
   한 건만: `--asset <asset_id>`. 결과의 `requeued`는 새 검증 작업 ID, `skipped`는 건드리지 않은 까닭
   (`VERIFY_JOB_ACTIVE` 진행 중 · `NOT_RECEIVED` 이미 다른 상태 · `LOCKED` 다른 처리가 잡고 있음 — 잠시 뒤 다시 돌리면 된다),
   `failed`는 넣지 못한 자산과 예외 종류다(`error_type`, 종료 코드 1). `failed`의 자산은 작업도 감사도 남지 않았으니 DB 연결 등
   원인을 확인한 뒤 같은 명령을 다시 돌린다 — 이미 넣은 자산은 다시 고르지 않는다.
   표준 출력은 결과 JSON 하나이고 경고·실패 안내는 표준 오류로 나온다(`... > result.json`으로 저장해 읽어도 된다).
   명령이 본 `MALWARE_SCANNER`가 `none`인데 검사가 필요한 자산이 있으면 경고가 나온다 — 명령은 `.env`를 읽으므로,
   워커를 다시 띄웠는지 따로 확인한다.
   여러 번·동시에 돌려도 자산마다 대기·실행 중인 검증 작업은 하나다. 자산 상태는 바꾸지 않는다(등록·정산은 워커가 한다).
4. 원인이 남아 있으면 새 작업도 같은 방식(5회)으로 FAILED가 된다. 2번을 다시 확인한 뒤 3번을 다시 돌린다.

**완료 확인:** 1번 dry-run의 `stuck`이 비어 있다. 다시 넣은 자산이 `REGISTERED`(영상) 또는 `READY_PRIVATE`가 되고 영수증
(`GET /v1/assets/{asset_id}/receipt`)이 나온다. 기관 사용량(`GET /v1/usage`)의 예약이 줄고 사용량이 그만큼 늘었다(한 번만).
감사 로그에 `INTEGRITY_VERIFY_REQUEUED`가 자산마다 사유와 함께 남았다.
