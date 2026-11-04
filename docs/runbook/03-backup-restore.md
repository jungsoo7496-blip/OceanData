# 03 백업·복원

> 기준선 구현(ADR-0021). 운영 도구(pgBackRest·rclone·restic)는 PoC 후 교체. PITR·백업 암호화·오래된 회차 정리는 아직 없다.

**판단 기준:** 정기 백업(`BACKUP_ENABLED=true`, `BACKUP_INTERVAL_H`), 장애로 복구가 필요할 때, 분기 복원 시험(Q_RESTORE).

**자동 대응:** 워커가 주기마다 `backup` 작업 → `backup_runs` 기록, 실패하면 운영자 메일(`backup.failed`). 정기 점검 D_BACKUP 자동 판정.

**사람이 할 것**
1. 지금 백업: `make backup` 또는 `POST /v1/backup-runs`(운영자). 결과 `GET /v1/backup-runs`.
2. 복원 시험: `make restore-test` → 임시 DB·임시 위치에 복원해 행 수·감사 로그 체인·객체 sha256 대조, 끝나면 임시 DB·객체 삭제.
3. 실제 복구: 백업 버킷 `runs/<bkr_…>/db.dump`를 빈 DB에 `pg_restore --no-owner`, 객체는 `objects/<존>/<키>`를 각 존 버킷 `<키>`로 복사 → `make verify-audit`·`make verify-integrity`.
4. 운영 설정: `BACKUP_S3_ENDPOINT_URL`(다른 장비·위치), `BACKUP_DATABASE_URL`(소유자 계정), `PG_DUMP_BIN`·`PG_RESTORE_BIN`(서버 주버전과 같게). API 이미지에는 pg_dump가 아직 없다 — 확인 필요.

**완료 확인:** 복원 시험 `ok: true`, `GET /v1/backup-runs`의 restore_test 결과.
