# 03 백업·복원

> 기준선 구현(ADR-0021). 운영 도구(pgBackRest·rclone·restic)는 PoC 후 교체. PITR·백업 암호화·오래된 회차 정리는 아직 없다.

**판단 기준:** 정기 백업(`BACKUP_ENABLED=true`, `BACKUP_INTERVAL_H`), 장애로 복구가 필요할 때, 분기 복원 시험(Q_RESTORE).

**자동 대응:** 워커가 주기마다 `backup` 작업 → `backup_runs` 기록, 실패하면 운영자 메일(`backup.failed`). 정기 점검 D_BACKUP 자동 판정.

**사람이 할 것**
1. 지금 백업: `make backup` 또는 `POST /v1/backup-runs`(운영자). 결과 `GET /v1/backup-runs`.
2. 복원 시험: `make restore-test` → 임시 DB·임시 위치에 복원해 행 수·감사 로그 체인·객체 sha256 대조, 끝나면 임시 DB·객체 삭제.
3. 실제 복구: 백업 버킷 `runs/<bkr_…>/db.dump`를 빈 DB에 `pg_restore --no-owner`, 객체는 `objects/<존>/<키>`를 각 존 버킷 `<키>`로 복사 → `make verify-audit`·`make verify-integrity`.
4. 실행 환경: `docker compose --env-file .env -f deploy/compose/docker-compose.yml build migrate`로 공용 이미지를 다시 빌드한다.
   PostgreSQL 16 `pg_dump`·`pg_restore`가 들어 있으며, 도구·서버의 주버전이 다르면 백업·복원을 중단하고 조치 안내를 기록한다 (ADR-0022).
5. 운영 설정: `BACKUP_S3_ENDPOINT_URL`(다른 장비·위치), `BACKUP_S3_ACCESS_KEY_ID`·`BACKUP_S3_SECRET_ACCESS_KEY`,
   `BACKUP_BUCKET`, `BACKUP_ENABLED`·`BACKUP_INTERVAL_H`를 `.env`에 지정한다.
   `BACKUP_DATABASE_URL`은 백업·임시 DB 생성 권한을 갖춘 계정으로 지정한다.
   개발 Compose에서 비우면 `migrate`·`worker`에 `MIGRATION_DATABASE_URL`을 전달한다. 일반 API에는 이 소유자 연결을 넘기지 않는다.
   설정을 바꾸면 `docker compose --env-file .env -f deploy/compose/docker-compose.yml up -d --force-recreate worker`로 반영한다.
6. 도구 확인: `docker compose --env-file .env -f deploy/compose/docker-compose.yml run --rm migrate pg_dump --version`과
   같은 명령의 `pg_restore --version`이 모두 16인지 확인한다. 컨테이너 밖에서는 `PG_DUMP_BIN`·`PG_RESTORE_BIN`을
   서버와 같은 주버전 도구 경로로 지정한다. T10도 이 설정 경로를 사용한다.

**완료 확인:** 복원 시험 `ok: true`, `GET /v1/backup-runs`의 restore_test 결과.
