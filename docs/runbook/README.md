# 운영 Runbook (17번 요구사항 5)

장애·작업 때 이 절차를 그대로 따른다. 각 문서는 **판단 기준 → 자동 대응 → 사람이 확인할 것 → 완료 확인** 순서다.
명령은 저장소 루트에서 실행한다 (`COMPOSE="docker compose --env-file .env -f deploy/compose/docker-compose.yml"`).
운영 배포 형태(서버·스토리지)는 하드웨어 확정 후 이 문서의 명령을 바꾼다 (tech.md 결정 대기).

| 문서 | 상황 |
|---|---|
| [01-start-stop.md](01-start-stop.md) | 기동·중지 |
| [02-deploy-update.md](02-deploy-update.md) | 배포 갱신(이미지·마이그레이션) |
| [03-backup-restore.md](03-backup-restore.md) | 백업·복원 (08번 백업 PoC 후 확정) |
| [04-credential-revoke.md](04-credential-revoke.md) | 장비 자격증명·API 키 폐기 |
| [05-break-glass.md](05-break-glass.md) | 긴급 열람 (02번 요구사항 8 구현 후 확정) |
| [06-storage-failure.md](06-storage-failure.md) | 스토리지 장애 |
| [07-queue-stopped.md](07-queue-stopped.md) | 작업 큐 정지·적체 |
| [08-disk-threshold.md](08-disk-threshold.md) | 디스크·쿼터 용량 임계 |
| [09-token-leak.md](09-token-leak.md) | 토큰·비밀값 유출 의심 |
| [10-dataset-revoke.md](10-dataset-revoke.md) | 데이터셋·이용 권한 철회 |

**수행 기록 (T14):** 스테이징에서 절차를 실제로 해 본 뒤 `records/YYYY-MM-DD-<문서번호>.md`에 수행자·시각·결과·이상 사항을 남긴다
(양식: [records/TEMPLATE.md](records/TEMPLATE.md)).
