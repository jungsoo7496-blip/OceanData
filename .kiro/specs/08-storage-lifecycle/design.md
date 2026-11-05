# 설계: 08 저장·보존·백업

## 이번에 구현한 것 (마이그레이션 0007)
| 구성 | 구현 |
|---|---|
| 보존 정책 | `retention_policies`(대상 RAW·PROXY·THUMBNAIL·CLIP·DEID·SENSOR_EXPORT·EVIDENCE·DATASET·QUARANTINE:<사유> → 기간·DELETE/KEEP·예고 일수). 등록·파생·격리 때 `apply_retention()`이 retention_policy_id·retention_until 계산. 무기한은 9999-12-31. **기간 값은 초안(10년·격리 30일) — 운영기관 확정 필요** |
| 만료 실행 | `dry_run()`(대상·용량·법적 보류 제외 수·인용 경고), `execute_expired()`(기관별 삭제 증명, 인용 중 자산 건너뜀). 워커가 하루 한 번 예고 알림 + dry-run 기록. `RETENTION_AUTO_EXECUTE=true`면 자동 실행, 기본은 운영자가 `python -m app.cli retention --execute` |
| 법적 보류 | `assets.legal_hold`·사유. `POST /v1/assets/{id}/legal-hold`(운영자·MFA·사유·감사). 보류 자산은 만료·삭제 요청 모두에서 제외 |
| 삭제 | `POST /v1/assets/{id}/deletion`·`/v1/sessions/{id}/deletion`(ORG_ADMIN·MFA). 계보로 파생본까지, 활성 객체 삭제, 사용량 차감, 자산 기록은 `deleted_at`으로 남기고 모든 조회에서 숨김. `deletion_certificates`(dlc_): 객체 키·sha256·크기, 백업 사본 소멸 예정일(`BACKUP_RETENTION_DAYS`) |
| 재수신 | 멱등 키 유일 제약을 `deleted_at IS NULL` 부분 인덱스로 바꿔 삭제 뒤 같은 파일을 다시 받을 수 있다 |
| 쿼터 알림 | 예약·확정·격리 때 사용률이 80·90·100%를 처음 넘으면 ORG_ADMIN 알림 (`org_quotas.last_alert_pct`) |
| 사용량 | `GET /v1/usage`: 등록 객체·격리·예약·백업 항목별. 제공기관은 자기 기관, 운영자는 전체 |
| 일일 대조 | 기관 접두사로 RAW·PROCESSED·EVIDENCE·QUARANTINE 실측 합계 ↔ DB 사용량, 차이 시 운영자 알림. `daily_tasks` + advisory lock으로 하루 한 번 |

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
- `GET /v1/usage-reports?month=YYYY-MM`(ORG_ADMIN 자기 기관, 운영자 전체): 그 달 마지막 스냅숏의 존별 저장량, 처리 시간(작업 마지막 시도의
  locked_at~updated_at, CPU/GPU), 다운로드 URL 발급 수·대상 크기(실제 전송량 아님), 데이터셋 배포 수. 금액은 넣지 않는다.

## 서버 측 암호화 (요구사항 1.3·1.4)
`S3_SSE=AES256`이면 `init-buckets`가 존별 버킷에 기본 암호화(SSE-S3)를 요청한다(객체마다 헤더를 붙이지 않아 사전 서명 업로드도 적용).
API 기동 때와 `/readyz`(5분 캐시)에서 `GetBucketEncryption`으로 확인해, 켜지지 않았거나 제품이 지원하지 않으면 경고 로그와
`/readyz` `warnings: ["storage_encryption_unsupported:<존>"]`. 준비 상태(200/503)는 바꾸지 않는다. 주소·자격증명은 내보내지 않는다.

