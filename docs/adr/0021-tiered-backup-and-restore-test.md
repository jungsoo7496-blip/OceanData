# ADR-0021: 계층형 백업과 복원 시험 — 내장 기준선, 운영 도구는 PoC 뒤 포트 교체

- 상태: accepted (pgBackRest·rclone·restic PoC 후 일부 superseded 예정)
- 날짜: 2026-11-04
- 관련: 08번 요구사항 6·7, domain.md 9장(backup_class), OSS-CANDIDATES.md(pgBackRest·rclone·restic는 PoC 3순위), T10-a·T10-b

## 맥락
08번은 다시 만들 수 없는 데이터만 별도 대상에 정기 백업하고(RAW 전체는 하지 않는다), `make restore-test`로 임시 환경에 복원해
DB 행 수·객체 해시를 대조하라고 한다. 운영 도구 후보(pgBackRest 시점 복구, rclone 객체 복사, restic 암호화 스냅샷)는 PoC가
끝나지 않았다. 그렇다고 백업이 없는 상태로 두면 정기 점검 D_BACKUP·복원 시험(T10)을 확인할 수 없다.

## 결정
1. **백업 대상은 한 술어로**: `backup_class=PROTECTED` 이거나 EVIDENCE·DATASET 존이거나 variant=DEID인 자산(삭제 증명의
   백업 소멸일 판단과 같은 술어), 그리고 EVIDENCE·DATASET 버킷의 자산이 아닌 객체(보고서 스냅샷·매니페스트·배포 매니페스트).
   backup_class=NONE인 RAW는 넣지 않는다. 메타데이터 DB(감사 로그·정책·설정 표 포함)는 `pg_dump` 사용자 정의 형식으로 통째로.
2. **대상 저장소는 별도 S3 설정**: `BACKUP_S3_ENDPOINT_URL`·`BACKUP_S3_ACCESS_KEY_ID`·`BACKUP_S3_SECRET_ACCESS_KEY`·`BACKUP_BUCKET`.
   비우면 같은 S3의 `backup` 버킷(개발용 — 운영에서는 다른 장비·위치 필수). 객체는 `objects/<버킷>/<키>`에 증분 복사(크기·sha256이
   같으면 건너뜀)하고, 복사 뒤 백업 쪽을 다시 읽어 sha256을 확인한다. 회차마다 `runs/<bkr_…>/manifest.json`(객체 키·sha256·크기,
   표별 행 수, 덤프 sha256)과 `runs/<bkr_…>/db.dump`.
3. **DB 덤프와 행 수는 같은 스냅샷**: REPEATABLE READ 트랜잭션에서 `pg_export_snapshot()` → 같은 트랜잭션에서 표별 행 수 →
   `pg_dump --snapshot`. 그래서 복원본 행 수와 매니페스트 행 수가 정확히 같아야 한다. `pg_dump`는 서버와 같은 주버전이어야 해서
   경로를 `PG_DUMP_BIN`·`PG_RESTORE_BIN`으로 설정한다(compose에서는 postgres 컨테이너의 것을 쓴다).
4. **결과 기록·알림**: `backup_runs` 표(bkr_): 상태(RUNNING·SUCCEEDED·FAILED)·대상 수·복사 수·용량·덤프 키·오류. 실패하면
   운영자에게 알림(11번 notifier). 정기 점검 D_BACKUP은 최근 성공 회차로 자동 판정. 운영자 조회 `GET /v1/backup-runs`.
5. **복원 시험(`make restore-test`)**: 최근 성공 회차를 임시 DB(`restore_<회차>`)에 `pg_restore`하고 표별 행 수를 매니페스트와 대조,
   객체는 백업 쪽에서 임시 버킷으로 내려받아 sha256을 매니페스트·원본 자산 기록과 대조한 뒤 임시 DB·버킷을 지운다. 결과는
   JSON(`var/restore-test.json`)과 `backup_runs.restore_test`에 남긴다.
6. **포트**: 객체 복사와 DB 덤프는 `services/backup.py`의 작은 함수 둘(`_copy_objects`, `_dump_db`) 뒤에 있다. PoC에서 rclone·
   pgBackRest가 채택되면 이 둘만 바꾸고, 대상 술어·매니페스트·대조·기록은 그대로 둔다.

버린 대안: (a) PoC 전까지 백업 없음 — T10·D_BACKUP 확인 불가. (b) 객체 버킷 전체 복제 — RAW를 백업하지 않는다는 결정(domain.md 9장)과 어긋나고 비용이 크다. (c) ORM으로 표를 JSON 내보내기 — 시퀀스·제약·트리거(감사 로그 수정 금지)가 복원되지 않는다.

## 결과
- 좋아지는 점: 백업 대상이 삭제 증명·사용량과 같은 규칙을 쓰고, 복원 시험이 자동으로 행 수·해시를 대조한다.
- 감수할 점: 시점 복구(PITR)와 백업 암호화·보존 정리(오래된 회차 객체 삭제)는 아직 없다 — PoC 결과로 정한다. 대용량 객체는
  임시 파일을 거쳐 복사하므로 작업 디스크가 객체 하나만큼 필요하다.
- 되돌리는 방법: `backup_runs` 마이그레이션 down, 설정 제거. 백업 대상 버킷은 사람이 지운다(자동 삭제하지 않음).
