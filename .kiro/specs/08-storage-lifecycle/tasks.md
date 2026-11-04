# 작업: 08 저장·보존·백업

- [x] 1. 존별 버킷·객체 키·파일명 미포함 — 요구사항 1.1, 1.2 (04번에서 구현, 계속 사용)
- [ ] 2. 서버 측 암호화 요청·미지원 시 기동 경고와 /readyz 표시 — 요구사항 1.3, 1.4
- [x] 3. 쿼터 정책 표·항목별 사용량·사용량 API — 요구사항 2.1, 2.2, 2.5, 2.6 · 시험 `test_quota_alerts_usage_and_reconcile`
- [x] 4. 일일 실측 대조·차이 알림 — 요구사항 2.3
- [x] 5. 80·90·100% 알림 — 요구사항 2.4
- [x] 6. 보존 정책 표·등록 시 계산·격리 사유별 — 요구사항 3.1, 3.2 · 시험 `test_retention_policies_applied`
- [x] 7. 만료 예고 알림 — 요구사항 3.3 · 시험 `test_expiry_notification`
- [x] 8. 만료 실행·삭제 기록·dry-run API·운영자 확인 후 실행 — 요구사항 3.4, 3.6, 3.7 · 시험 `test_dry_run_legal_hold_and_expiry_execution`
- [ ] 9. DATASET 존 사본을 원 자산 만료와 별도로 — 요구사항 3.5 (13번에서 데이터셋 생기면, 정책 표에는 DATASET KEEP 있음)
- [x] 10. 법적 보류 설정·해제(운영자·사유·감사)·삭제 제외 — 요구사항 4.1, 4.2
- [x] 11. 자산·회차 삭제·파생본 포함·삭제 증명·백업 소멸일 — 요구사항 5.1, 5.2 · 시험 `test_asset_deletion_*`, `test_session_deletion_and_cross_org`
- [ ] 12. GRANTED 데이터셋 포함 시 409·증빙 인용 영향 목록과 근거 손상 표시 — 요구사항 5.3, 5.4 (증빙·보고서 인용은 10번에서 완료: `retention.affected_by()` → 409·영향 목록, 근거 손상 표시. 데이터셋은 13번)
- [ ] 13. 계층형 백업 작업(DB·감사·EVIDENCE·DEID·DATASET·PROTECTED·설정 → 별도 S3) — 요구사항 6.1~6.3, 6.5 (OSS PoC 3순위: pgBackRest + rclone (+restic) 후 결정)
- [ ] 14. ROTATING 등 원천 장비 자산 백업 신청 → PROTECTED·예상 용량·사용량 반영 — 요구사항 6.4 (org_quotas.backup_bytes 항목은 있음)
- [ ] 15. `make restore-test`·복원 Runbook — 요구사항 7.1, 7.2 (T10, 13번 작업 이후)
- [ ] 16. 용량 예측(일별 추세·70% 도달 예상일·리드타임 알림) — 요구사항 8.1, 8.2
- [ ] 17. Hot/Warm 계층 설정·Tier_Mover(복사→sha256 확인→원본 삭제·위치 갱신, 이동 중 조회 유지) — 요구사항 9.1~9.4
- [ ] 18. 기관별 월간 자원 사용 리포트(P1) — 요구사항 10.1, 10.2
