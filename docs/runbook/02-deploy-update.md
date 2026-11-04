# 02 배포 갱신

**판단 기준:** 릴리스 게이트(`make release-gate`)를 통과한 버전을 올릴 때.

**자동 대응:** CI가 계약·보안·라이선스·마이그레이션 up/down/up·수용시험을 확인한다. 마이그레이션은 `migrate` 서비스가 기동 때 적용한다.

**사람이 할 것**
1. 릴리스 노트(`make release-notes`)에서 바뀐 계약·알려진 제한·하위호환 기간 확인.
2. 백업 확인 (03 Runbook) — 마이그레이션 전 DB 백업.
3. `git pull` → `make up` (이미지 다시 빌드, 마이그레이션 적용).
4. 문제가 있으면 이전 태그로 되돌리고 `$COMPOSE run --rm migrate alembic downgrade <이전 리비전>` (모든 마이그레이션에 down 있음).

**완료 확인:** `/readyz` 200, `/v1/system/capabilities` 기능 표시, 대표 흐름 하나(업로드 → 등록) 성공, 수집 모니터링에 새 장애 없음.
