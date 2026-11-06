# 설계: 08 저장·보존·백업

## 이번에 구현한 것 (마이그레이션 0007)
| 구성 | 구현 |
|---|---|
| 보존 정책 | `retention_policies`(대상 RAW·PROXY·THUMBNAIL·CLIP·DEID·SENSOR_EXPORT·EVIDENCE·DATASET·QUARANTINE:<사유> → 기간·DELETE/KEEP·예고 일수). 등록·파생·격리 때 `apply_retention()`이 retention_policy_id·retention_until 계산. 무기한은 9999-12-31. **기간 값은 초안(10년·격리 30일) — 운영기관 확정 필요** |
| 만료 실행 | `dry_run()`(대상·용량·법적 보류 제외 수·인용 경고), `execute_expired()`(기관별 삭제 증명, 인용 중 자산 건너뜀, 대상 행을 먼저 잠금). 워커가 하루 한 번 예고 알림 + dry-run 기록. `RETENTION_AUTO_EXECUTE=true`면 자동 실행, 기본은 운영자가 `python -m app.cli retention --execute` |
| 법적 보류 | `assets.legal_hold`·사유. `POST /v1/assets/{id}/legal-hold`(운영자·MFA·사유·감사). 보류 자산은 만료·삭제 요청 모두에서 제외 |
| 삭제 | `POST /v1/assets/{id}/deletion`·`/v1/sessions/{id}/deletion`(ORG_ADMIN·MFA). 계보로 파생본까지, 활성 객체 삭제, 사용량 차감, 자산 기록은 `deleted_at`으로 남기고 모든 조회에서 숨김. `deletion_certificates`(dlc_): 객체 키·sha256·크기, 백업 사본 소멸 예정일(`BACKUP_RETENTION_DAYS`). 원 자산 행을 잠근(asset_id 순, 계보 단계마다) 뒤 파생본을 찾는다 — 기록 중인 파생 작업(구간 클립)의 결과도 함께 지운다 (ADR-0033 갱신) |
| 재수신 | 멱등 키 유일 제약을 `deleted_at IS NULL` 부분 인덱스로 바꿔 삭제 뒤 같은 파일을 다시 받을 수 있다 |
| 쿼터 알림 | 예약·확정·격리 때 사용률이 80·90·100%를 처음 넘으면 ORG_ADMIN 알림 (`org_quotas.last_alert_pct`) |
| 사용량 | `GET /v1/usage`: 등록 객체·격리·예약·백업 항목별. 제공기관은 자기 기관, 운영자는 전체 |
| 일일 대조 | 기관 접두사로 RAW·PROCESSED·EVIDENCE·QUARANTINE 실측 합계 ↔ DB 사용량, 차이 시 운영자 알림. `daily_tasks` + advisory lock으로 하루 한 번 |

## 업로드와 자산·회차 삭제 (ADR-0046)

- 원본·파생 자산을 먼저 잠근 뒤 `UPLOADING` 세션을 `SKIP LOCKED`로 잠근다. 완료·만료와 경합 중이거나 세션을 확인할 수 없으면 409를 반환한다. `RECEIVED`·`VALIDATING` 자산이 대상에 있으면 검증이 끝날 때까지 삭제를 409로 거부한다.
- 멀티파트 중단 뒤 완성 객체가 없음을 확인하면 세션을 `ABORTED`로 기록하고 쿼터 예약을 해제한다. 자산은 삭제 증명에 연결해 소프트 삭제한다. 미완성 객체는 증명의 객체 목록과 등록·격리 사용량 차감에서 제외한다. 이미 완성된 객체가 발견되면 409, 저장소 오류가 나면 `Retry-After`와 함께 503을 반환한다.
- 보존 만료 미리 보기와 실행은 업로드·수신·검증 중인 자산을 제외한다. 삭제 요청에서만 멀티파트 중단을 시도한다.

## 확인 필요 / 다음 스펙과 연결
- `affected_by()`: 승인된 증빙·보고서(10번)·이용 권한 있는 데이터셋 버전(13번) 인용 조회 — 그 스펙에서 채운다 (요구사항 3.6 경고, 5.3 거부, 5.4 재확인·근거 손상 표시)
- 알림 수신자는 사용자 이름까지다. 메일 주소·메신저 채널은 IdP·운영기관 확정 후 (tech.md 결정 대기)

## 백업·복원 실행 환경 보완 (2026-11-04, ADR-0022)
- 공용 이미지에 PGDG `postgresql-client-16`을 설치하고 두 도구의 절대경로를 명시한다.
- 덤프·복원 전에 서버와 도구의 주버전을 대조한다. 불일치는 기존 백업 실패 또는 복원 시험 실패 결과로 기록한다.
- Compose가 백업 설정을 전달한다. 백업용 소유자 연결은 `migrate`·`worker`에만 전달하고 개발 기본값은 `MIGRATION_DATABASE_URL`이다.
- 계약·도메인 코드값·DB 모델 변경 없음. 새 마이그레이션(down 포함)은 해당 없음이다.
- 검증: 버전 호환성 단위시험 12개, T10 응답 계약·증분 백업·손상 검사·복원, 실제 비루트 이미지 CLI 백업·복원.

## 기관 백업 신청 (요구사항 6.4)
`POST /v1/backup-requests`(ORG_ADMIN, 기본 dry_run=true): local_copy_policy가 ROTATING·NONE·UNKNOWN인 자기 기관 장비의 RAW 자산 중 아직 NONE인 것
(회차·촬영 구간으로 좁힐 수 있음)의 개수·용량과 신청 뒤 사용량을 보여준다. dry_run=false면 쿼터 안에서만 PROTECTED + `backup_requested_at`(0017),
용량을 `org_quotas.backup_bytes`에 더하고 감사(BACKUP_REQUESTED_BY_ORG). 다음 백업 회차 대상(ADR-0021 술어). 자산을 지우면 신청 용량도 뺀다.
장비의 local_copy_policy는 연계 카드(03번 작업 17)에서 들어온다.

## 용량 예측·월간 리포트 (요구사항 8, 10)
- 워커가 하루 한 번 `usage_snapshots`(0018)에 기관별 쿼터 장부(등록·격리·예약·신청 백업)·존별 활성 자산 크기·독립 백업 대상 크기를 남긴다.
- `GET /v1/capacity-forecast`(운영자): 최근 `CAPACITY_WINDOW_DAYS`(30) 스냅숏 합계의 최소제곱 기울기로 `CAPACITY_THRESHOLD_RATIO`(0.7) 도달 예상일.
  전체 용량 `STORAGE_CAPACITY_BYTES`가 없으면 예측일 없이 '확인 필요'. 예상일이 `CAPACITY_LEAD_TIME_DAYS`(180) 안이면 하루 한 번 운영자 메일.
- `GET /v1/usage-reports?month=YYYY-MM`(ORG_ADMIN 자기 기관, 운영자 전체): 그 달 마지막 스냅숏의 존별·계층별 저장량, 처리 시간(작업 마지막 시도의
  locked_at~updated_at, CPU/GPU), 다운로드 URL 발급 수·대상 크기(실제 전송량 아님), 데이터셋 배포 수. 금액은 넣지 않는다.
  계층별은 스냅숏의 `by_tier`(0022), 그 전 스냅숏은 모두 HOT.

## 서버 측 암호화 (요구사항 1.3·1.4)
`S3_SSE=AES256`이면 `init-buckets`가 존별 버킷에 기본 암호화(SSE-S3)를 요청한다(객체마다 헤더를 붙이지 않아 사전 서명 업로드도 적용).
API 기동 때와 `/readyz`(5분 캐시)에서 `GetBucketEncryption`으로 확인해, 켜지지 않았거나 제품이 지원하지 않으면 경고 로그와
`/readyz` `warnings: ["storage_encryption_unsupported:<존>"]`. 준비 상태(200/503)는 바꾸지 않는다. 주소·자격증명은 내보내지 않는다.
계층 이동을 켜면 WARM 버킷도 같은 방식으로 확인한다(`storage_encryption_unsupported:WARM:<존>`).

## Hot/Warm 계층·Tier_Mover (요구사항 9, 마이그레이션 0022, ADR-0036)
| 구성 | 구현 |
|---|---|
| 계층 설정 (9.1) | HOT = `S3_*`·`BUCKET_*`. WARM = `WARM_S3_*`(비우면 HOT과 같은 S3 연결)·`WARM_BUCKET_RAW/PROC/EVIDENCE/DATASETS`(비우면 `<HOT 버킷>-warm`). 격리 존은 HOT에만. 같은 주소에서 WARM 버킷 = HOT 버킷이면 기동 실패. `TIERING_ENABLED`(기본 false). 컨테이너 `warm_storage`(None이면 HOT 저장소 공유) |
| 위치 해석 (9.3) | 자산 행 (storage_tier, storage_zone, storage_key), 키는 계층과 무관. 읽는 곳은 모두 `services/tiers.locate()`: 다운로드 URL·`local_copy`(처리·비식별·태깅·클립·검수 프레임·데이터셋 샘플)·센서 파싱·수신/표본 무결성 검증·데이터셋 사본·백업·삭제(쓰는 모든 계층)·격리/재검증 이동(`relocate`, 언제나 HOT으로)·일일 대조(WARM 버킷 포함). 새 객체는 언제나 HOT. 긴 읽기가 유예 뒤 실패하면 지금 위치에서 한 번 다시 (`tiers.download`·백업 `_read`) |
| 정책 표 (9.2) | `tier_policies`(존·변형·min_age_days(ingested_at)·min_idle_days(last_accessed_at, 없으면 ingested_at)·enabled). HOT → WARM만. 초안 `raw_original_to_warm`(RAW·ORIGINAL·180·90) **꺼짐 — 운영기관 확정 필요**. `assets.last_accessed_at`은 다운로드 URL 발급·검수 프레임·클립 요청 때 갱신 |
| 이동 기록 | `tier_moves`(move_id bigint, from/to, 존·키·크기·sha256, 상태, error_code, legal_hold, switched_at·delete_after·finished_at). 자산마다 열린 이동(COPYING·SWITCHED) 하나 (부분 유일 인덱스) |
| 순서 (9.2·9.3) | 정비 주기마다 `TIER_MAX_IN_FLIGHT`까지 COPYING 커밋 → 작업 `tier_move`(0022 정책: 우선순위 0·임대 6시간·3회) → 같은 공간 표식 확인 → GET 스트림 → PUT/멀티파트 → 사본을 다시 읽어 sha256·크기 = 자산 기록 → 자산 행 잠금·변경 없음 확인 → storage_tier 전환(SWITCHED) → `delete_after = 전환 + max(TIER_DELETE_GRACE_S, PRESIGN_DOWNLOAD_TTL_S)` 뒤, 전환 전에 시작한 처리 작업이 없으면 원래 객체 삭제(DONE) |
| 상태 | COPYING → SWITCHED → DONE / COPYING → FAILED(COPY_CHECKSUM_MISMATCH·SOURCE_CHECKSUM_MISMATCH·SOURCE_MISSING·TIER_SAME_LOCATION·재시도 소진) / COPYING → CANCELLED(ASSET_DELETED·ASSET_RELOCATED·ASSET_CHANGED·TIERING_DISABLED). FAILED·CANCELLED는 사본만 지우고 원래 객체·위치는 그대로 |
| 오류 처리 | 해시 불일치는 PermanentJobError(재시도 없음), 원본 손상·없음은 운영자 알림 `tier.source_problem`·다시 고르지 않음, 사본 불일치는 `TIER_FAILED_RETRY_AFTER_S` 뒤 다시. 저장소 일시 오류는 작업 재시도. 작업을 못 넣은 이동은 10분 뒤 재등록. 살아 있는 자산의 다른 쪽 객체는 물리적으로 다른 공간인지 확인한 뒤에만 지운다 |
| 법적 보류 | 보류 자산도 옮긴다(삭제 금지일 뿐, 확인된 같은 사본이 있을 때만 원래 객체 삭제). 이동 당시 보류 여부를 `tier_moves.legal_hold`에. 매체 보존 요구는 법률 자문 확인 필요 |
| 사용량 | `usage_snapshots.by_tier` → `storage_by_tier`. 일일 대조는 잠시 두 벌인 객체를 뺀다 |
| 운영 | API 추가 없음. `python -m app.cli tiers`(dry-run), `--recall`(WARM → HOT), `init-buckets`(켰을 때 WARM 버킷), 메트릭 `tier_moves_total`·`tier_move_bytes_total`, `/readyz` 경고 `storage_tier_unavailable:WARM` |
| 계약 | 설명만 변경: UsageReport.storage_by_tier(계층별 합), `/v1/usage-reports` 설명, Readiness.warnings 코드 |
| 마이그레이션 | 0022 up: 위 열·표·인덱스·초안 규칙·작업 정책. down: WARM 자산·열린 이동이 있으면 멈춤(먼저 `tiers --recall`), 대기 작업·정책 삭제 |
| 시험 | `tests/integration/test_tiers.py`(moto): 정책 나이·조회 조건·dry-run, 이동 중(사본 확인 뒤 전환 직전) URL·읽기는 HOT, 전환 뒤 이전 URL 유예 중 유효·새 URL WARM, 유예 뒤 원래 객체 삭제, 다시 실행 멱등, 사본 손상·원본 손상·재시도 간격, 법적 보류 이동·삭제 거부, WARM에서 `local_copy`·무결성 표본·센서 파싱·백업(계획 뒤 이동)·데이터셋 사본, 유예 중·복사 중 삭제, 계층별 스냅숏·월간 리포트·일일 대조, 같은 저장소 공유 설정·처리 작업 중 삭제 미룸·되돌리기·이동 끄면 취소·작업 재등록, 같은 공간 다른 주소 거부, `/readyz` 경고, 0022 down 보호. `tests/unit/test_tier_settings.py`: 설정·유예·위치 기본값·스트림 리더 |

확인 필요: 실제 WARM 장비·제품·엔드포인트·대역폭, 운영기관 규칙 값, 보류 자산 매체 보존 요구(법률 자문). 데이터셋 버전 사본과
자산이 아닌 객체(보고서 스냅숏·외부 원 응답)는 옮기지 않는다. WARM → HOT 자동 승격은 없다(운영자 `--recall`).

