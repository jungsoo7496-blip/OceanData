# 계약 문구 대비 구현 점검 — 발견 목록 (2026-11-05)

`contracts/openapi.yaml`의 작업별 설명(상태 코드 조건·감사·범위 규칙)과 구현을 비교해 나온 불일치다. 묶음마다 발견 담당 에이전트가
임시 DB에서 **직접 재현**했고(재현 파일은 `repro/contract-sweep/`), 적대적 검증 단계는 주간 한도로 돌지 못했다 — 그래서 아래는
**재현됨·검증자 검토 전**이다. 고치기 전에 한 번 더 확인하고, 계약 문구가 틀린 경우는 계약부터 고친다 (AGENTS.md 3번).
발견 문구는 원문(영어)을 그대로 둔다. 심각도는 발견자의 판단이다.

## 2026-11-12 (KST) 첫 묶음 구현 반영

- **#2 보존 모의 실행** — 각 경고에 `reason`을 채운다. 승인된 회차 기록과 승인·제출된 보고서의 인용은 `CITED_BY_APPROVED_EVIDENCE`, `GRANTED` 이용 권한이 있는 데이터셋 버전은 `IN_DATASET_VERSION`을 쓴다. 인용 상세를 제공하고 데이터셋 경고에는 이용 권한 철회 안내를 포함한다.
- **#3 이벤트 구간 겹침 자산** — 이벤트와 자산의 조회 권한을 모두 확인한다. 기관 API 키에 `status:read`와 `asset:read`가 모두 필요하며, 장비 토큰은 조회할 수 없다. 자산 목록과 촬영시각 미상 개수에 Tenant_Guard를 적용한다.
- **#6 장비 관측값 매핑 조회** — 장비의 기관 범위를 먼저 확인해 다른 기관 장비 요청에 404를 반환하고 `CROSS_ORG_ACCESS_ATTEMPT`를 기록한다. 같은 기관의 수집기는 매핑된 원천 장비만 조회할 수 있다.
- **#7 이벤트 수정** — 요청에 포함된 `ended_at`은 `Z`로 끝나는 유효한 RFC 3339 시각이어야 한다. 형식 오류나 명시적 `null`은 400 `VALIDATION_ERROR`와 `/ended_at` 오류를 반환한다.
- **#8 관측값 정정** — 필수 `observed_at`에 같은 시각 검증을 적용하고, 잘못된 입력은 400 `VALIDATION_ERROR`와 `/observed_at` 오류를 반환한다.

## 2026-11-12 (KST) 둘째 묶음 구현 반영

- **#4 외부 소스 수정** — `enabled`, `interval_s`, `attribution`, `redistribution_allowed`에 명시적 `null`을 보내면 저장 전에 400 `VALIDATION_ERROR`를 반환한다. `rate_limit_per_min: null`은 한도를 해제한다. 응답과 DB 커밋 순서는 지시서 5번에 남긴다.
- **#15 긴급 열람 결정** — 두 명의 운영자가 필요한 설정에서 자신의 요청을 승인하거나 거절하려 하면 403과 `PERMISSION_DENIED` 감사를 남긴다. 감사는 거부된 요청 ID와 열람 승인 건을 가리킨다.
- **#16·#17 수집기 원격 상태·정책** — 수집기의 다른 기관 장비 요청은 404와 `CROSS_ORG_ACCESS_ATTEMPT` 감사를 남긴다. 같은 기관의 다른 수집기 및 없는 장비의 조회도 404를 반환한다.
- **#18 원천 없는 수집기의 기본 정책** — 원래 점검의 계약에 어긋난 200은 ADR-0040 반영 뒤 422로 재현됐다. 초기 수집기가 하트비트를 보낼 수 있게 기본 정책 `pv-0`에서 빈 `sources`와 `watch_rules`를 허용한다. 저장 정책은 원천 장비가 한 대 이상 있어야 한다. ADR-0051에 기록했다.

## 2026-11-12 (KST) 셋째 묶음 구현 반영

- **#9 이벤트 등록** — 같은 멱등 키와 본문은 최초 201 또는 200과 `Idempotent-Replayed` 헤더를 받는다. 같은 키에 다른 유효한 본문은 409를 받는다. 원천 ID 중복은 키가 없거나 새 키인 경우 기존 이벤트를 200으로 돌려준다.
- **#10 관측값 배치** — 원천 장비 또는 회차가 범위 밖이면 전체 배치를 404로 거부한다. 장비가 다른 기관의 원천이나 회차를 요청하면 `CROSS_ORG_ACCESS_ATTEMPT`를 기록한다. 같은 기관의 미매핑 원천은 `DEVICE_SCOPE_VIOLATION`을 기록한다. 기존 항목별 결과 코드 목록은 유지한다.
- **#11 사용량 조회** — 운영자 결과에 쿼터 행이 없는 기관도 표시한다. 기본 쿼터와 사용량 0을 표시하며 조회로 쿼터 행을 만들지 않는다.
- **#13 점검 생성** — 기간의 운영 시간대 기한을 UTC로 표현할 수 없으면 `/period_key` 오류가 있는 400을 반환한다. 점검 기록과 생성 감사는 남기지 않는다.
- **#19 하트비트** — 수집기 식별자 불일치의 404와 미지원 스키마 버전의 422를 계약에 명시했다. 다른 기관 수집기 요청은 동일한 404와 `CROSS_ORG_ACCESS_ATTEMPT` 감사를 남기고 상태를 갱신하지 않는다.

## 2026-11-12 (KST) 넷째 묶음 구현 반영

- **#21 에이전트 등록** — 미발급 코드의 401 `UNAUTHENTICATED`를 계약에 명시한다. 전체 계약 시험은 OpenAPI `security` 선언으로 인증이 필요한 작업을 식별해 401·403·429를 검사한다.
- **#22 등록 코드 발급** — 원천 장비의 422와 폐기 상태 수집기의 409를 계약에 명시한다.
- **#23 장비 등록** — 조회 범위 밖 참조의 404, 다른 본문으로 같은 멱등 키를 쓸 때의 409, 원천 장비를 수집기로 지정할 때의 422를 계약에 명시한다. 동시 요청은 지시서 5번에서 다룬다.
- **#24 약정 종료** — 종료 시각이 시작보다 빠를 때의 422를 계약에 명시한다.
- **#25 운영자 장비 등록 거부** — 기관 관리자 권한이 없는 운영자의 장비 등록은 403과 `PERMISSION_DENIED` 감사를 남긴다. 감사의 요청 ID로 거절 응답을 대조할 수 있다.

## 2026-11-12 (KST) 다섯째 묶음 구현 반영

- **#12 겹치는 자산** — 촬영 시작 시각과 자산 ID로 정렬해 커서로 이어 본다. 기본 페이지 크기는 500건이며 촬영시각 미상 개수는 전체 필터 범위에 적용한다. 타임라인의 더 보기에서 다음 페이지를 요청한다(ADR-0052).
- **#26 긴급 열람 철회** — ADR-0029에 따라 `REQUESTED`는 `DENIED`, `ACTIVE`는 `EXPIRED`로 바뀐다고 OpenAPI에 명시했다.
- **#27 자격증명 폐기** — 원천 장비 요청은 상태·등록 코드 변경 전에 422로 거부한다. 이전 호출로 재등록 대기가 된 원천 장비가 발견되면 최초 상태의 근거를 확인한 뒤 처리한다.

## status·events·observations (상태·이벤트·관측값) — 점검한 작업 39개, 발견 13건

### 1. [high] POST /v1/backup-runs — missing-behavior

- **계약:** summary: "지금 백업 (PLATFORM_OPERATOR). 작업 큐에 넣고 바로 돌려준다". 202 returns the job_id of the queued backup; 409 only when a backup is already running.
- **구현:** Every enqueue uses the constant dedupe_key="backup". uq_jobs_dedupe_key is a unique index over all job rows whose dedupe_key is not null, so it also covers DONE and FAILED jobs. On a conflict PgQueue.enqueue returns the existing job_id. Once any backup job has ever existed, each later POST returns 202 with that old (DONE/FAILED) job_id and writes the BACKUP_REQUESTED audit, but queues nothing. The worker's scheduled backup (backup.due → enqueue with the same key) is blocked the same way, so after the first backup job neither manual nor scheduled backups run.
- **근거:** backend/app/api/lifecycle.py:48; backend/app/adapters/pg_queue.py:46,54-57; backend/app/db/models.py:121; backend/app/db/migrations/versions/0001_foundation_and_identity.py:77; backend/app/workers/main.py:300-302; backend/app/services/backup.py:298-303
- **재현 순서:** As user 106: POST /v1/backup-runs → 202 {job_id: J1}. Let the worker finish it, or run UPDATE jobs SET status='DONE' WHERE job_id=J1. POST /v1/backup-runs again → 202 with job_id == J1. SELECT count(*) FROM jobs WHERE job_type='backup' → 1, with 0 QUEUED. Reproduced in a scratch run.

### 2. [medium] POST /v1/retention/dry-run — data-semantics

- **계약:** In the RetentionDryRun schema, each warnings item has required [asset_id, reason], with reason enum [CITED_BY_APPROVED_EVIDENCE, IN_DATASET_VERSION] (contracts/openapi.yaml:5015-5039).
- **구현:** warnings = retention.affected_by(...). That returns dicts {asset_id, cited_by: RECORD|REPORT|DATASET, id, version} or {asset_id, cited_by: DATASET, id, grant_id, action}. There is no `reason` key and none of the enum values. Any dry-run that has a cited or dataset-held asset breaks the response schema. Dataset warnings are produced only for versions with a GRANTED grant, not for every dataset version.
- **근거:** backend/app/services/retention.py:205-223 (line 219), 117-134; backend/app/services/evidence.py:146-147; contracts/openapi.yaml:5015-5039; tests/integration/test_dataset_retention.py:71 asserts the cited_by/grant_id keys
- **재현 순서:** Insert an org-A asset with retention_policy_id='raw_default' and retention_until=now-1d. Add an APPROVED session_record_version that cites it (evidence_citations, citing_type RECORD). POST /v1/retention/dry-run as 106 → 200 with warnings=[{asset_id, cited_by:'RECORD', id, version}]. assert_matches_contract fails with "'reason' is a required property". Reproduced.

### 3. [medium] GET /v1/events/{event_id}/overlapping-assets — scope-or-permission

- **계약:** "보는 사람의 자산 범위(Tenant_Guard) 안에서만" (openapi.yaml:2753). domain.md 2장, the table Tenant_Guard implements and outside which access is denied: DEVICE sees only "자기 수집기·매핑된 원천 장비의 정책·상태" metadata. Asset metadata for machine principals is otherwise gated by scope asset:read.
- **구현:** The endpoint is guarded only by event:read, which needs scope status:read. Every DEVICE token has that scope (DEVICE_SCOPES), and so does any API key holding status:read without asset:read. scope_filter gives DEVICE and API_KEY org-wide scope. The response embeds full asset_view() objects (checksum, source_location, env_context, provenance and so on) for every org asset in the session, including assets of source devices not mapped to the calling collector. A device can create an event in a mapped source's name with an arbitrarily wide [started_at, ended_at] and so list every asset of the session.
- **근거:** backend/app/api/observations.py:410-440 (414 require("event:read"), 423 scoped_select(Asset, p), 438 asset_view); backend/app/domain/permissions.py:63,94; backend/app/domain/principal.py:7-8; backend/app/services/tenant.py:37-38; .kiro/steering/domain.md:43
- **재현 순서:** Enroll DEV_A_COLLECTOR and get a device token. Insert an org-A SOURCE device that is not mapped to this collector, with an asset in SES_A. With the device token, GET /v1/assets → 403 and GET /v1/assets/{id} → 403. Then POST /v1/events {source: DEVICE, source_system_id: DEV_A_CAMERA, started_at: -30d, ended_at: +30d} → 201. GET /v1/events/{id}/overlapping-assets → 200 and includes the unmapped device's asset with its full metadata. Reproduced.

### 4. [medium] PATCH /v1/external-sources/{source_id} — wrong-status

- **계약:** In the request body, enabled is boolean, interval_s is integer ≥60, attribution is string and redistribution_allowed is boolean, none of them nullable. A format error should be 400. A 200 returns the stored ExternalSource, in which enabled is a required boolean.
- **구현:** SourcePatch declares every field Optional, so explicit nulls pass validation. The handler setattr()s them onto NOT NULL columns without flushing and returns 200 with the nulls echoed back. get_db commits only after the response has been sent: the NOT NULL violation rolls back the whole transaction, and the server logs "response already started". Valid fields sent in the same body are silently dropped, while the EXTERNAL_SOURCE_UPDATED audit is still recorded.
- **근거:** backend/app/api/external.py:103-109, 119-131; backend/app/api/deps.py:32-41; backend/app/db/models.py:1166,1172-1175
- **재현 순서:** As 106, create source khoa-test. PATCH /v1/external-sources/khoa-test {"enabled": false, "interval_s": null} → 200 with body enabled=false, interval_s=null. GET /v1/external-sources → still enabled=true, interval_s=600, and audit_log has EXTERNAL_SOURCE_UPDATED. Reproduced.

### 5. [medium] POST /v1/external-sources/{source_id}/backfill — missing-behavior

- **계약:** summary "과거 구간 보충 수집 등록"; 202 body jobs = "등록한 수집 작업 수".
- **구현:** Each window is enqueued with dedupe_key collect:{id}:{start}:{end}. The jobs unique index spans all statuses, so on a conflict PgQueue returns the existing (possibly DONE or FAILED) job, but `jobs` is incremented anyway. Re-requesting a window whose earlier jobs finished or failed returns jobs=N while registering nothing, so the gap is never re-collected.
- **근거:** backend/app/api/external.py:154-160; backend/app/adapters/pg_queue.py:46,54-57; backend/app/db/models.py:121
- **재현 순서:** As 106: POST /v1/external-sources/khoa-test/backfill {from: now-2d, to: now-1d} → 202 {jobs: 1}. Run UPDATE jobs SET status='FAILED' WHERE job_type='collect_source'. Repeat the same POST → 202 {jobs: 1}, yet the jobs table still has 1 collect_source row and 0 QUEUED. Reproduced.

### 6. [medium] GET /v1/devices/{device_id}/observation-mapping — missing-audit

- **계약:** 404 = "없음 또는 다른 기관 리소스". security.md:10: "다른 기관의 리소스를 요청하면 ... 404를 돌려주고 감사 로그에 남긴다". Cross-org attempts and permission denials are mandatory audit events.
- **구현:** For a DEVICE principal, any device_id outside mapped_source_ids raises NOT_FOUND before load_scoped. That includes other organizations' devices. Neither CROSS_ORG_ACCESS_ATTEMPT nor DEVICE_SCOPE_VIOLATION is recorded. By contrast, a user token making the same request records CROSS_ORG_ACCESS_ATTEMPT, and POST /v1/observations records DEVICE_SCOPE_VIOLATION.
- **근거:** backend/app/api/observations.py:238-240 (compare 49-54); .kiro/steering/security.md:10
- **재현 순서:** With a DEV_A_COLLECTOR device token: GET /v1/devices/dev_00000000000000000000000312/observation-mapping → 404 and no new audit_log row. The same call as user 101 → 404 plus 1 CROSS_ORG_ACCESS_ATTEMPT. Reproduced.

### 7. [medium] PATCH /v1/events/{event_id} — wrong-status

- **계약:** EventPatch.ended_at is utc_datetime (RFC 3339 ending in Z). Documented responses are 200/400/401/403/404/429, so malformed input → 400 VALIDATION_ERROR.
- **구현:** EventPatch.ended_at is a plain, unvalidated str. parse_rfc3339 raises TimeRuleError (a ValueError) for a value with no offset or a malformed value. No exception handler maps it, so the request ends as 500 INTERNAL.
- **근거:** backend/app/api/observations.py:443-449, 468-469; backend/app/domain/normalize.py:25-37; backend/app/errors.py:147-151
- **재현 순서:** Create an event as 102, then PATCH /v1/events/{id} {"ended_at": "2026-11-01T10:00:00"} → 500 INTERNAL. {"ended_at": "garbage"} → 500. Reproduced.

### 8. [medium] POST /v1/observations/corrections — wrong-status

- **계약:** ObservationCorrection.observed_at is utc_datetime; a format error → 400 (BadRequest is documented).
- **구현:** Correction.observed_at is an unvalidated str. services.observations.correct() calls parse_rfc3339, which raises TimeRuleError (a ValueError) that is not handled, giving 500 INTERNAL.
- **근거:** backend/app/api/observations.py:201-210, 213-218; backend/app/services/observations.py:150
- **재현 순서:** As 101: POST /v1/observations/corrections {source_system_id: DEV_A_SENSOR, metric: sea_water_temperature, observed_at: "2026-11-01T03:00:00", value: 1.0, reason: "test reason"} → 500 INTERNAL. With observed_at "not-a-time" → 500. Reproduced.

### 9. [low] POST /v1/events — wrong-status

- **계약:** Idempotency-Key: "같은 키·같은 본문은 처음 응답을 다시 준다 (Idempotent-Replayed 헤더). 같은 키·다른 본문은 409 IDEMPOTENCY_CONFLICT". 409 is documented.
- **구현:** The source_event_id duplicate lookup runs before run_idempotent and returns 200 with the existing event. A same-key, same-body retry gets 200 without Idempotent-Replayed, although the first response was 201. A same-key, different-body request gets 200 instead of 409.
- **근거:** backend/app/api/observations.py:353-358, 379-381
- **재현 순서:** As 102 with header Idempotency-Key: evt-key-0001: POST /v1/events with source_event_id src-evt-1 → 201. Repeat it unchanged → 200 with no Idempotent-Replayed header. Repeat with only `note` changed → 200 (contract: 409). Reproduced.

### 10. [low] POST /v1/observations — wrong-status

- **계약:** Documented responses are 200/400/401/403/409/413/422/429, with no 404. The per-item reason enum (observation_reject_reason) includes DEVICE_SCOPE and SESSION_NOT_FOUND.
- **구현:** The whole batch gets 404 NOT_FOUND when source_system_id belongs to another org or is not mapped to the calling collector, or when session_id is not visible. The DEVICE_SCOPE and SESSION_NOT_FOUND item reasons are never produced anywhere in the code.
- **근거:** backend/app/api/observations.py:49-58, 84-87; contracts/openapi.yaml:2530-2557; contracts/schemas/common.schema.json:416-425
- **재현 순서:** As 102: POST /v1/observations with source_system_id=DEV_B_CAMERA → 404, and assert_matches_contract reports "계약에 404 응답이 없다". A device token posting for an unmapped own-org source → 404. Reproduced.

### 11. [low] GET /v1/usage — data-semantics

- **계약:** "기관 사용량 (항목별). 제공기관은 자기 기관, 운영자는 전체 기관". organization-policies says its usage equals /v1/usage.
- **구현:** Lists only organizations that already have an org_quotas row. A provider with no row gets a synthesized default row, but the operator gets nothing for such orgs. On a fresh system the operator sees items: [] while 5 orgs exist.
- **근거:** backend/app/api/lifecycle.py:25-32 (compare backend/app/api/admin_policies.py:57-62)
- **재현 순서:** On a fresh seed: GET /v1/usage as 106 → {"items": []}. As 103 → one default ORG_B row. GET /v1/organization-policies as 106 → 5 orgs. Reproduced.

### 12. [low] GET /v1/events/{event_id}/overlapping-assets — data-semantics

- **계약:** Returns the assets whose capture window overlaps the event window, in capture start order. No pagination, cap or truncation flag is defined; required keys are event_id, session_id, window, items, unknown_capture_count.
- **구현:** The query has .limit(500), so overlapping assets beyond 500 are dropped silently with no indicator or cursor. Many SAMPLE_FRAME/CLIP assets plus margin_s up to 3600 make this reachable.
- **근거:** backend/app/api/observations.py:427-429
- **재현 순서:** Insert 505 SES_A assets with capture windows inside one event's [started_at, ended_at]. GET /v1/events/{id}/overlapping-assets → 200 with exactly 500 items and no truncation signal. Reproduced.

### 13. [low] POST /v1/inspections — wrong-status

- **계약:** period_key pattern '^[0-9]{4}(-[0-9]{2}(-[0-9]{2})?|-W[0-9]{2}|-Q[1-4])?$', so "9999-12-31" and "9999" are valid. Responses are 201/400/409.
- **구현:** Only period_last_day() is validated. inspections.create() then computes tpl.due_at, which adds one day to the period's last date: date(9999,12,31)+1d raises OverflowError, which goes unhandled → 500 INTERNAL.
- **근거:** backend/app/api/monitoring.py:250-258; backend/app/domain/inspections.py:83-86; backend/app/services/inspections.py:131-141
- **재현 순서:** As 106: POST /v1/inspections {"cycle": "DAILY", "period_key": "9999-12-31"} → 500. {"cycle": "YEARLY", "period_key": "9999"} → 500. For comparison, "2026-02-30" → 400. Reproduced.

**발견자 메모(검토 범위·공통 원인):**

> I checked 39 of the 40 operations tagged status, events and observations. GET /v1/quality-reports was skipped because it is out of scope. All 13 findings were reproduced against a throwaway database (marine_sweep_seo, since dropped) using two scratch test files:
> - /tmp/claude-0/-home-user-oceandata/804cf8c5-3e8a-5d79-bc09-75a711922000/scratchpad/sweep/status-events-observations/test_sweep_seo.py
> - /tmp/claude-0/-home-user-oceandata/804cf8c5-3e8a-5d79-bc09-75a711922000/scratchpad/sweep/status-events-observations/test_sweep_seo2.py
>
> Raw output is in out.txt and out2.txt in the same folder. Nothing in the repository was changed.
>
> Two root causes are shared:
> - **Job queue dedupe:** the unique index on jobs.dedupe_key covers every status. That one fact breaks both the backup-runs finding and the backfill finding, and it also stops scheduled backups (workers/main.py:302).
> - **Commit after the response:** get_db commits only after the response has gone out. Any handler that doesn't flush can therefore return 200 for a change the database then rejects. The external-sources PATCH is the case I could show.
>
> Checked and left out (minor, cross-cutting or ambiguous):
> - **Device tokens read org-wide.** DEVICE tokens always carry status:read, so they can read the whole organization through GET /v1/ingest-status (devices that aren't mapped to them appear), /v1/events, /v1/incidents, /v1/usage and /v1/observations. domain.md 2장 and spec 02 requirement 5.5 limit devices to their own collector and mapped sources. I reported this only for overlapping-assets, the one operation whose text cites Tenant_Guard.
> - **Operator reads aren't audited.** domain.md 2장 says operator metadata reads are "감사 기록", but no read handler in this group writes an audit row. No operation text in the group references that rule.
> - **Nulls accepted for non-nullable fields** with only minor effects: PATCH /v1/organization-policies {"quota_bytes": null, "reason": ...} returns 200 with no change and no audit, while the sibling retention and job PATCHes answer 400 not_nullable. PATCH /v1/incidents {"assignee": null} and PATCH /v1/watch-rules {"severity": null} are also accepted.
> - **interval without metric:** GET /v1/observations with interval but no metric returns 400, which the contract doesn't document.
> - **AGENT failure reasons:** in ingest-status, top_failure_reasons counts AGENT errors with no 24-hour window. The 24-hour description could be read as covering only the three sources it lists.
>
> Everything else matched the contract. That includes:
> - the incident 409 transition and cross-org audit
> - watch-rule params checks and MFA
> - the retention-policy action_locked 409, 404 and DELETE-needs-days rules
> - the job-policy priority update for QUEUED jobs, with max_attempts applying only to new jobs
> - the org-policy over_quota flag and the matching usage sum
> - the capacity-forecast '확인 필요' nulls
> - usage-reports scope
> - backup-requests: 404, 422 for policy and quota, dry_run defaulting to true
> - backup-runs ordering
> - the inspection templates, list, get, patch and evidence endpoints
> - hardware-signals
> - the mapping PUT audit
> - the PAYLOAD_TOO_LARGE check before validation and the per-item results of POST /v1/observations
> - the scoping and filters of GET /v1/events and GET /v1/events/{event_id}

## context·agents·auth·system (맥락·에이전트·인증·시스템) — 점검한 작업 43개, 발견 14건

### 14. [medium] POST /v1/devices — data-semantics

- **계약:** createDevice takes the IdempotencyKey parameter (openapi.yaml:4505-4509): '같은 키·같은 본문은 처음 응답을 다시 준다 (Idempotent-Replayed 헤더)… 24시간 보관' (also domain.md 7장, api-conventions.md).
- **구현:** run_idempotent looks the key up with a plain db.get and inserts the IdempotencyRecord only after fn() has run. Nothing serializes requests that share a key; only POST /v1/codes/event-categories/subcodes takes a pg_advisory_xact_lock. A retry that arrives while the first request is still in flight therefore runs create() again: it gets a new device_id and its DEVICE_REGISTERED audit commits in a separate transaction. The idempotency-row PK conflict appears only in get_db's db.commit(). FastAPI 0.162 runs that after the response has already been sent (fastapi/routing.py:148-155). Observed result: both callers got 201 with different device_ids and no Idempotent-Replayed header. Only one device row persists, so one client holds a device_id that does not exist, and the audit log has 2 DEVICE_REGISTERED rows.
- **근거:** backend/app/services/idempotency.py:35-47; backend/app/api/context.py:102-122; backend/app/api/catalog.py:69-72 (the only endpoint that serializes per key); backend/app/api/deps.py:32-36 (commit in yield teardown)
- **재현 순서:** As user 101, send POST /v1/devices {name:'race cam',kind:'SOURCE',device_type:'CAMERA',local_copy_policy:'RETAINED'} with Idempotency-Key: retry-key-dev-0001 twice concurrently. Send the second while the first is still inside create(); the probe widened that window by delaying device_view 1.5s. Observed: both 201 with different device_id values. SELECT device_id FROM devices WHERE name='race cam' returns only one of them, and audit_log has 2 DEVICE_REGISTERED rows.

### 15. [medium] POST /v1/break-glass-grants/{grant_id}/decision — missing-audit

- **계약:** decideBreakGlass: '긴급 열람 승인·거절 (다른 PLATFORM_OPERATOR, 다중 인증)', so a self-approval is refused with 403. security.md:27 '감사 로그 (반드시 기록): … 권한 거부 … 긴급 열람'; ADR-0029 결정 3 '요청·승인·거절·조기 종료·만료를 각각 감사'.
- **구현:** bg.decide raises Refused('FORBIDDEN') when requested_by == decider. The router turns it into ProblemError with no audit, because BREAK_GLASS_* is written only after a successful decide. The four-eyes bypass attempt leaves no audit row at all. By contrast, permission-table denials record PERMISSION_DENIED in require().
- **근거:** backend/app/services/break_glass.py:72-73; backend/app/api/break_glass.py:78-87; backend/app/api/deps.py:76-86 (require() audits table-level denials)
- **재현 순서:** As 106 (mfa), POST /v1/break-glass-grants {target_type:'SESSION',target_id:'ses_00000000000000000000000401',reason:'INC-0002 probe reason text',duration_s:600}. Then, still as 106 (mfa), POST /v1/break-glass-grants/{grant_id}/decision {decision:'APPROVE'} with X-Request-Id: 01JZZZZZZZZZZZZZZZZZZZZZZ1. Result: 403 FORBIDDEN, and SELECT * FROM audit_log WHERE request_id='01JZZZZZZZZZZZZZZZZZZZZZZ1' returns 0 rows.

### 16. [medium] GET /v1/agents/{device_id}/health — missing-audit

- **계약:** 404 = NotFound '없음 또는 다른 기관 리소스 (둘을 구분하지 않는다)'; security.md:10 '다른 기관의 리소스를 요청하면 … 404를 돌려주고 감사 로그에 남긴다' (02번 요구사항 2.2).
- **구현:** For a DEVICE principal the handler returns 404 as soon as device_id != p.device_id, before load_scoped runs. A device credential probing another organization's collector therefore leaves no CROSS_ORG_ACCESS_ATTEMPT row. The same request from a user principal goes through load_scoped and is audited.
- **근거:** backend/app/api/agents.py:49-51; backend/app/api/deps.py:93-102
- **재현 순서:** Enroll dev_00000000000000000000000301 as 101 (mfa): POST .../enrollment-codes, then POST /v1/agents/enroll, then POST /v1/auth/token. With that device token, GET /v1/agents/dev_00000000000000000000000311/health returns 404 and adds 0 CROSS_ORG_ACCESS_ATTEMPT rows. The same GET as user 102 returns 404 and adds 1 CROSS_ORG_ACCESS_ATTEMPT row.

### 17. [medium] GET /v1/devices/{device_id}/policy — missing-audit

- **계약:** 404 = NotFound '없음 또는 다른 기관 리소스'; security.md:10 says another org's resource gets 404 plus an audit record.
- **구현:** Same short-circuit as the health endpoint: a DEVICE principal asking for any device_id other than its own gets 404 before load_scoped, so cross-org probes by device credentials are not audited (CROSS_ORG_ACCESS_ATTEMPT is missing).
- **근거:** backend/app/api/catalog.py:109-111; backend/app/api/deps.py:93-102
- **재현 순서:** With a device token for dev_00000000000000000000000301 (obtained as in the previous finding), GET /v1/devices/dev_00000000000000000000000311/policy returns 404 and adds 0 CROSS_ORG_ACCESS_ATTEMPT rows.

### 18. [medium] GET /v1/devices/{device_id}/policy — data-semantics

- **계약:** The 200 body is './schemas/device-policy.schema.json', which requires 'sources' with minItems: 1 (device-policy.schema.json:11-13).
- **구현:** When no policy version is stored, default_policy builds sources only from devices whose collector_device_id is this collector and that have a project_id; the collector itself is never included. A newly registered collector, or one whose sources lack project_id, therefore gets "sources": []. The project's own checker (tests/contract/openapi_contract.assert_matches_contract) reports '200 계약 위반' for this response.
- **근거:** backend/app/services/device_policy.py:41-53 (line 48); backend/app/api/catalog.py:114-116; contracts/schemas/device-policy.schema.json:11-13
- **재현 순서:** As 101 (mfa), POST /v1/devices {name:'probe collector',kind:'COLLECTOR',device_type:'AGENT_PC',local_copy_policy:'RETAINED',project_id:'prj_00000000000000000000000201'}. Then GET /v1/devices/{new_id}/policy returns 200 with sources: [] (schema error: /sources minItems). This is the normal onboarding order: register the collector, enroll it, and the agent fetches its policy before any sources are mapped.

### 19. [low] POST /v1/agents/heartbeat — wrong-status

- **계약:** postHeartbeat lists responses 204, 400, 401, 403 and 429 only.
- **구현:** Returns 404 NOT_FOUND when body.collector_device_id is not the caller's collector. This includes another org's collector, and that case is not audited either (same pattern as the GET endpoints above). Also returns 422 UNSUPPORTED_SCHEMA_VERSION for a different major version or an unknown higher minor. Neither status is documented; assert_matches_contract reports '계약에 404 응답이 없다' and '계약에 422 응답이 없다'.
- **근거:** backend/app/api/agents.py:23-25, 30-31
- **재현 순서:** With a device token for dev_00000000000000000000000301, POST /v1/agents/heartbeat using contracts/examples/agent-heartbeat.vessel-agent.json with collector_device_id=dev_00000000000000000000000311 returns 404. With collector_device_id=dev_00000000000000000000000301 and schema_version '2.0.0' it returns 422.

### 20. [low] POST /v1/sessions — data-semantics

- **계약:** IdempotencyKey parameter: '같은 키·같은 본문은 처음 응답을 다시 준다 (Idempotent-Replayed 헤더)'.
- **구현:** Same unserialized run_idempotent as POST /v1/devices. A concurrent retry with the same key and body runs create() again, blocks on the org_label unique index until the first request commits, then hits IntegrityError and returns 409 CONFLICT ('같은 label의 실증회차가 이미 있습니다') instead of replaying the 201. A sequential retry afterwards replays correctly.
- **근거:** backend/app/api/context.py:174-192; backend/app/services/idempotency.py:35-47
- **재현 순서:** As 102, send POST /v1/sessions {project_id:'prj_00000000000000000000000201',label:'IDEM-RACE-1',title:'race',starts_at:'2026-11-03T00:00:00Z'} with Idempotency-Key: retry-key-0001 twice concurrently, the second while the first is in flight. Observed: the first returns 201 and the second returns 409 CONFLICT without Idempotent-Replayed.

### 21. [low] POST /v1/agents/enroll — wrong-status

- **계약:** enrollAgent lists responses 201, 400, 410 and 429 (no 401).
- **구현:** An unknown enrollment code returns 401 UNAUTHENTICATED; used or expired codes return 410. tests/integration/test_identity.py:108 asserts this 401, but the contract was never updated, and the endpoint is in the Schemathesis SKIP set.
- **근거:** backend/app/api/auth.py:111-118
- **재현 순서:** POST /v1/agents/enroll {enrollment_code:'unknown-code-123',agent_version:'0.3.0',os:'linux'} returns 401. assert_matches_contract reports '계약에 401 응답이 없다'.

### 22. [low] POST /v1/devices/{device_id}/enrollment-codes — wrong-status

- **계약:** createEnrollmentCode lists responses 201, 401, 403, 404 and 429.
- **구현:** A non-collector device returns 422 RULE_VIOLATION (unmet condition kind=COLLECTOR). A REVOKED device returns 409 CONFLICT, though no current API sets REVOKED, so that path is unreachable. Neither status is documented.
- **근거:** backend/app/api/auth.py:80-85
- **재현 순서:** As 101 (mfa), POST /v1/devices/dev_00000000000000000000000302/enrollment-codes (a SOURCE camera) returns 422. assert_matches_contract reports '계약에 422 응답이 없다'.

### 23. [low] POST /v1/devices — wrong-status

- **계약:** createDevice lists responses 201, 400, 401, 403 and 429; 409 is implied only by the Idempotency-Key parameter text.
- **구현:** A collector_device_id or project_id that does not exist, or belongs to another org, returns 404 NOT_FOUND via load_scoped. A collector_device_id that points to a SOURCE device returns 422 RULE_VIOLATION. Neither status is documented.
- **근거:** backend/app/api/context.py:103-110
- **재현 순서:** As 101 (mfa), POST /v1/devices {name:'probe',kind:'SOURCE',device_type:'CAMERA',local_copy_policy:'RETAINED',collector_device_id:'dev_00000000000000000000000302'} returns 422. The same body with project_id:'prj_ZZZZZZZZZZZZZZZZZZZZZZZZZZ' instead returns 404.

### 24. [low] POST /v1/agreements/{agreement_id}/end — wrong-status

- **계약:** endAgreement: 'valid_until을 effective_at(기본 지금)으로 당긴다'. Responses are 200, 400, 401, 403, 404, 409 and 429 (no 422).
- **구현:** end_agreement raises RuleViolation when effective_at < valid_from, which becomes 422 RULE_VIOLATION. This includes the default case: ending a not-yet-started agreement with no effective_at (effective_at defaults to now) fails with this undocumented 422.
- **근거:** backend/app/services/onboarding.py:66-75; backend/app/api/onboarding.py:93
- **재현 순서:** As 106 (mfa), POST /v1/agreements {organization_id:'org_00000000000000000000000001',tier:'TIER1',reference:'AGR-PROBE-2',valid_from:'2027-01-01T00:00:00Z'}. Then POST /v1/agreements/{id}/end {reason:'probe end'} returns 422; assert_matches_contract reports '계약에 422 응답이 없다'.

### 25. [low] POST /v1/devices — missing-audit

- **계약:** Summary '장비 등록 (ORG_ADMIN)', so a PLATFORM_OPERATOR is refused with 403. security.md:27 lists 권한 거부 as an event that must be audited.
- **구현:** The permission table grants device:write to PLATFORM_OPERATOR, and the handler then raises FORBIDDEN itself with no PERMISSION_DENIED audit. A table-level denial for ORG_MEMBER is audited.
- **근거:** backend/app/api/context.py:98-100; backend/app/domain/permissions.py:42
- **재현 순서:** As 106 (mfa), POST /v1/devices {name:'op-dev',kind:'SOURCE',device_type:'CAMERA',local_copy_policy:'RETAINED'} with an X-Request-Id returns 403, with 0 audit_log rows for that request_id. The same request as 102 returns 403 and writes a PERMISSION_DENIED row.

### 26. [low] POST /v1/break-glass-grants/{grant_id}/revoke — data-semantics

- **계약:** Summary: '긴급 열람 조기 종료 (PLATFORM_OPERATOR, 다중 인증). 기간 만료와 같은 EXPIRED가 된다' (openapi.yaml:3000).
- **구현:** When the grant is still REQUESTED, revoke sets status DENIED and returns 200 with status DENIED, not EXPIRED. ADR-0029 결정 4 says '승인 전 철회는 DENIED', so the contract text is the side that is out of date.
- **근거:** backend/app/services/break_glass.py:82-89; contracts/openapi.yaml:3000; docs/adr/0029-break-glass.md:19
- **재현 순서:** As 106 (mfa), POST /v1/break-glass-grants {target_type:'SESSION',target_id:'ses_00000000000000000000000401',reason:'INC-0001 probe reason text',duration_s:600}. Then POST /v1/break-glass-grants/{id}/revoke {reason:'probe'} returns 200 with status 'DENIED'.

### 27. [low] POST /v1/devices/{device_id}/credentials/revoke — data-semantics

- **계약:** '수집기 자격증명 폐기 (ORG_ADMIN, 다중 인증 필요). 장비는 재등록 대기 상태가 된다': the re-enrollment state is meant for collectors.
- **구현:** There is no kind check. On a SOURCE device it revokes 0 credentials but still flips status from ACTIVE to PENDING_ENROLLMENT. Enrollment codes are refused for non-collectors (422), so the device can never return to ACTIVE.
- **근거:** backend/app/api/auth.py:140-155 (line 151); backend/app/api/auth.py:81-83
- **재현 순서:** As 101 (mfa), POST /v1/devices/dev_00000000000000000000000302/credentials/revoke returns 200 {revoked_credentials:0}. GET /v1/devices/dev_00000000000000000000000302 now shows status PENDING_ENROLLMENT (it was ACTIVE), and POST .../enrollment-codes for it returns 422.

**발견자 메모(검토 범위·공통 원인):**

> All 43 operations tagged context, agents, auth and system were compared against their handlers and services. Every finding above was reproduced on a temporary database (marine_sweep_ctxauth, dropped afterwards) using throwaway probe tests in /tmp/claude-0/-home-user-oceandata/804cf8c5-3e8a-5d79-bc09-75a711922000/scratchpad/sweep/context-agents-auth-system/ (test_probe_ctx*.py, raw output in probe_out*.jsonl). Where a status code was undocumented, the repo's own tests/contract/openapi_contract.assert_matches_contract also rejects the response. The repository was not modified.
>
> Cross-cutting observations behind several findings (not limited to this group):
> (1) get_db (backend/app/api/deps.py:32-36) commits in yield-dependency teardown, and FastAPI 0.162 runs that after the response is sent. Any commit-time failure therefore reaches the client as a success for a write that was rolled back.
> (2) run_idempotent has no per-key serialization. Only the org event subcode endpoint takes an advisory lock, so every run_idempotent caller (uploads, observations, events, datasets, sharing, reports) has the same retry race.
> (3) The SCHEMATHESIS_ALL sweep never runs as ORG_ADMIN (WHO lacks user 101) and skips POST /v1/agents/enroll. That is why the undocumented statuses on ORG_ADMIN-only endpoints and on enroll were not caught.
>
> Deliberately not reported:
> - the applyPolicyDraft expected_policy_version behaviour (out of scope);
> - the enrollment-code MFA requirement, which is not stated in the summary, but 403 MFA_REQUIRED is covered by the documented Forbidden response;
> - operators and PROGRAM_VIEWER seeing other orgs in listDevices/listProjects despite '자기 기관' summaries, which is consistent with the domain.md 2장 table;
> - the break-glass list being silently capped at 200 with no pagination in the contract;
> - operator metadata reads not being audited, which is a domain.md/02-2.1 requirement gap not tied to these operations' contract text;
> - SESSION_UPDATED and other success audits being committed outside the request transaction (general audit-atomicity topic).
>
> Everything else matched the contract text: role and MFA rules, cross-org 404 plus audit via load_scoped for user principals, API key and device credential immediate revocation, session PATCH semantics with before/after audit, integration card versioning and local_copy_policy propagation, the policy-draft create/patch/reject/apply rules (409 on revision or open draft, 422 on blockers, POLICY_* audits in the same transaction), agreements, break-glass request/approve/notify, the model registry (422 license, 409 duplicate), and session deletion (409 on legal hold, affected or granted).

## 점검하지 못한 묶음

- `datasets·sharing·deid·assets`(40개 작업), `reports·records·contracts·uploads`(30개 작업): 발견 담당이 주간 한도로 중단돼 결과를 내지 못했다.
  중간에 만든 재현 파일만 `repro/contract-sweep/` 아래에 남아 있다 — 이어서 점검할 때 참고한다.
