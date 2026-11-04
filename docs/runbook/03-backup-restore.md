# 03 백업·복원

> **확인 필요:** 08번 백업 작업(pgBackRest·rclone·restic PoC)이 아직 없다. 아래는 PoC 결과로 확정할 초안이다.

**판단 기준:** 정기(일·주) 백업, 장애로 데이터 복구가 필요할 때, 분기 복원 시험(정기 점검 Q_RESTORE).

**자동 대응(예정):** 백업 작업 성공·실패는 정기 점검 D_BACKUP 항목에 자동으로 채운다. 지금은 AUTO_UNAVAILABLE.

**사람이 할 것 (지금 가능한 범위)**
1. DB: `$COMPOSE exec postgres pg_dump -U marine_owner -Fc marine > backup-$(date +%F).dump`
2. 객체: PROTECTED 자산(`backup_class=PROTECTED`)·EVIDENCE·DATASET 버킷을 별도 S3로 복사 (도구 확정 후).
3. 복원 시험: 빈 DB에 `pg_restore` → `make verify-audit`·`make verify-integrity`.

**완료 확인:** 복원본에서 감사 로그 체인·무결성 로그 체인 정상, 표본 객체 sha256 일치 (T10-a는 08번 구현 후 `make restore-test`).
