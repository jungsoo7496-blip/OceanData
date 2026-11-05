# 수용시험 결과 (2026-11-05, 로컬 검증 누적)

> 백엔드 기준선은 2026-11-04 결과다. 2026-11-05 클라우드: T01-e를 실제 SeaweedFS로 확인해 PASS로 바꿨다 (docker 필요, 없으면 건너뜀). 2026-11-05에 T01-h 로컬 보안 보고서·T02-g 실제 브라우저 재개와 Go 전체 50건(MQTT 포함)·T03-b 에이전트 결과를 추가했다.
> T03-b는 실제 프로세스 강제 종료와 주입 시계 72시간 모의 단절 시험이며, 실제 72시간·PC 전원 차단·15번 엣지 장비 검증은 별도다.
> 다시 만들기: `make test-acceptance`, 보안 보고서는 `tools/security_acceptance.py`로 연결한다. 원본은 `var/acceptance-results.jsonl`이다.
> 이 표는 로컬 검증 범위이며 새 원격 GitHub Actions나 전체 릴리스 성공을 뜻하지 않는다. strict 라이선스 검사는 미상·승인 대기 항목 때문에 차단 중이다.
> 수용 ID 밖의 Windows 업데이트 native 시험 14개와 실제 제품 CLI 서명 갱신·롤백이 통과했다. OS 기동 지연 관찰과 운영 서비스 확인 범위는 ADR-0024에 기록했다.


| 상태 | 건수 |
|---|---|
| NEEDS_HARDWARE | 2 |
| NEEDS_INPUT | 4 |
| NOT_IMPLEMENTED | 5 |
| PASS | 73 |

| ID | 케이스 | 상태 | 시험 / 사유 |
|---|---|---|---|
| T01-a | A 기관 토큰으로 B 기관 자산 목록·상세·검색·프록시·다운로드 URL·감사 로그·관측값·이벤트 조회 | PASS | tests/acceptance/test_t01_tenant_isolation.py::test_T01_a_cross_org_resources_are_404_and_audited[GET-/v1/devices/dev_00000000000000000000000312-None]<br>tests/acceptance/test_t01_tenant_isolation.py::test_T01_a_cross_org_resources_are_404_and_audited[GET-/v1/devices/dev_00000000000000000000000311-None]<br>tests/acceptance/test_t01_tenant_isolation.py::test_T01_a_cross_org_resources_are_404_and_audited[GET-/v1/sessions/ses_00000000000000000000000411-None]<br>tests/acceptance/test_t01_tenant_isolation.py::test_T01_a_cross_org_resources_are_404_and_audited[POST-/v1/devices/dev_00000000000000000000000311/enrollment-codes-None]<br>tests/acceptance/test_t01_tenant_isolation.py::test_T01_a_cross_org_resources_are_404_and_audited[POST-/v1/devices/dev_00000000000000000000000311/credentials/revoke-None]<br>tests/acceptance/test_t01_tenant_isolation.py::test_T01_a_cross_org_resources_are_404_and_audited[POST-/v1/sessions-body5]<br>tests/acceptance/test_t01_tenant_isolation.py::test_T01_a_cross_org_resources_are_404_and_audited[POST-/v1/sessions-body6]<br>tests/acceptance/test_t01_tenant_isolation.py::test_T01_a_cross_org_resources_are_404_and_audited[POST-/v1/devices-body7]<br>tests/acceptance/test_t01_tenant_isolation.py::test_T01_a_cross_org_api_key_revoke<br>tests/acceptance/test_t01_tenant_isolation.py::test_T01_a_cross_org_incident_update_and_list |
| T01-b | 목록·검색·자동완성 결과 건수와 응답으로 B 기관 데이터 존재 추정 | PASS | tests/acceptance/test_t01_tenant_isolation.py::test_T01_b_lists_do_not_reveal_other_org<br>tests/acceptance/test_t01_tenant_isolation.py::test_T01_b_pagination_stays_in_scope |
| T01-c | 요청 본문·쿼리·헤더에 B 기관 organization_id를 넣음 | PASS | tests/acceptance/test_t01_tenant_isolation.py::test_T01_c_body_query_header_org_id_do_not_change_scope<br>tests/integration/test_identity.py::test_organization_id_in_query_or_header_is_rejected |
| T01-d | 만료·변조·다른 대상(audience) 토큰 | PASS | tests/integration/test_identity.py::test_user_token_validation_and_audit |
| T01-e | 사전 서명 URL의 객체 키·만료·서명 변조 | PASS | tests/acceptance/test_t01e_presigned_tamper.py::test_t01e_tampered_presigned_urls_are_rejected (일회용 SeaweedFS 3.80 컨테이너, 플랫폼 S3Storage.presign_get — 키·만료·서명·서명 시각·자격증명 변조와 만료 URL 모두 400/403, 원본 노출 0. 2026-11-05) |
| T01-f | PROGRAM_VIEWER가 L1 자산의 원본·프록시·다운로드 URL 요청 | PASS | tests/acceptance/test_t01_tenant_isolation.py::test_T01_f_program_viewer_scope |
| T01-g | 검수자·운영자 역할로 제공기관 승인을 대신하려 함 | PASS | tests/acceptance/test_t01_tenant_isolation.py::test_T01_g_operator_and_reviewer_cannot_act_as_provider_admin |
| T01-h | 동적 보안 검사(ZAP)·의존성 취약점 검사 | PASS | tools/security_acceptance.py::T01_h_local_scan_reports |
| T02-a | 감시 폴더에 합성 영상 저장 | PASS | agent/internal/core::TestT02FolderToServerWithoutHumanIntervention |
| T02-b | 쓰기 중인 파일 | PASS | agent/internal/core::TestWritingFileIsNotQueuedUntilSizeStable |
| T02-c | 장비 정책이 C3→C2로 바뀜 | PASS | tests/integration/test_ingest.py::test_variant_rules_and_metadata_only<br>agent/internal/core::TestPolicyChangeExcludesQueuedFilesAndReports |
| T02-d | C0(원본 이전 불가) 장비 | PASS | tests/integration/test_ingest.py::test_variant_rules_and_metadata_only |
| T02-e | 모의 RTSP 카메라 → 엣지 클립 → 업로드 | NOT_IMPLEMENTED | 15번 엣지 수집기 (조건부, 하드웨어·연계 카드 확정 후) |
| T02-f | 모의 NVR이 라이브 RTSP만 지원 | NOT_IMPLEMENTED | 15번 엣지 수집기 |
| T02-g | 웹 업로드 (탭 닫았다 열기 포함) | PASS | web/e2e/upload-resume.mjs::T02_g_tab_close_and_resume |
| T02-h | C1 원천 장비가 ORIGINAL을 보냄 | PASS | tests/integration/test_ingest.py::test_variant_rules_and_metadata_only |
| T03-a | 10GB 이상 파일 40% 지점 단절 후 재시작 | NEEDS_HARDWARE | 10GB 시험은 운영 스토리지·회선에서 (04번 tasks) |
| T03-b | 에이전트 강제 종료·72시간 오프라인 후 재개 (엣지 포함) | PASS | agent/internal/core::TestT03ForcedKillAnd72HourOfflineResume |
| T03-c | 업로드 세션 만료 | PASS | tests/integration/test_ingest.py::test_quota_abort_and_expiry |
| T04-a | 해시 불일치 | PASS | tests/integration/test_ingest.py::test_T04_checksum_mismatch_is_quarantined |
| T04-b | 확장자·MIME 위장 | PASS | tests/integration/test_ingest.py::test_T04_mime_mismatch_detected_by_content |
| T04-c | 같은 Idempotency-Key·같은 본문 | PASS | tests/integration/test_ingest.py::test_duplicates_and_idempotency |
| T04-d | 같은 키·다른 본문 | PASS | tests/integration/test_ingest.py::test_duplicates_and_idempotency |
| T04-e | 같은 (기관, 장비, sha256) 재전송 | PASS | tests/integration/test_ingest.py::test_duplicates_and_idempotency |
| T04-f | 작업 재시도·중복 실행 | PASS | tests/integration/test_pipeline.py::test_queue_retry_lease_and_resource_classes |
| T04-g | 바이너리 없는 C0 자산에 원본 업로드 (정책을 C3로 바꾼 뒤) | PASS | tests/integration/test_ingest.py::test_variant_rules_and_metadata_only |
| T05-a | 영상·센서·이벤트 정상 v1 예시 | PASS | tests/acceptance/test_t12_concurrency.py::test_t05_contract_examples_valid_and_invalid |
| T05-b | 촬영시각 근거 없음 (파일 수정 시각만 있음) | PASS | tests/unit/test_metadata_rules.py::test_capture_time_unknown_without_evidence |
| T05-c | 위경도 순서 뒤바뀜 | PASS | tests/unit/test_metadata_rules.py::test_swapped_lonlat_detected |
| T05-d | 관측값: KST 표기, 시간대 없음, 결측(null ≠ 0), 단위 오류, 미래 시각, 중복 | PASS | tests/integration/test_sensor_events.py::test_batch_item_results_and_rules |
| T05-e | `contracts/examples/invalid/` 전부 | PASS | tests/acceptance/test_t12_concurrency.py::test_t05_contract_examples_valid_and_invalid |
| T05-f | 사전 검증 API로 같은 문서 검사 | PASS | tests/integration/test_catalog.py::test_validate_endpoint_uses_same_rules |
| T06-a | 보고서 지표 → 회차 기록 → 이벤트·관측값 → 자산 → 원본 | PASS | tests/acceptance/test_t11_reports.py::test_t11_report_flow_and_t06a_trace |
| T06-b | 데이터셋 → 원본·모델 버전·검수자 | PASS | tests/acceptance/test_t06d_journey.py::test_t06d_three_providers_full_journey |
| T06-c | 원본 삭제·철회 | PASS | tests/acceptance/test_t08_t15_sharing.py::test_t08_t15_dataset_distribution |
| T06-d | 가상 3사(선박·CCTV·센서) 전체 여정 | PASS | tests/acceptance/test_t06d_journey.py::test_t06d_three_providers_full_journey |
| T06-e | 파생 자산(프록시·썸네일·클립·비식별본) → 원본 | PASS | tests/integration/test_catalog.py::test_lineage_and_asset_scope |
| T07-a | 에이전트 중지 | PASS | tests/acceptance/test_t07_collection_watch.py::test_t07a_agent_stop_raises_alert_and_incident |
| T07-b | 실증 예정일 무수집 | PASS | tests/acceptance/test_t07_collection_watch.py::test_t07b_scheduled_session_without_data |
| T07-c | 관측값 수신 지연·시계 오차 초과 | PASS | tests/acceptance/test_t07_collection_watch.py::test_t07c_observation_lag_and_clock_offset |
| T07-d | 기업 화면에 원인·조치 안내 | PASS | tests/acceptance/test_t07_collection_watch.py::test_t07a_agent_stop_raises_alert_and_incident |
| T08-a | CORE 자산을 L2로 | PASS | tests/integration/test_sharing.py::test_sharing_flow<br>tests/unit/test_sharing_rules.py::test_t08_a_core_cannot_reach_l2 |
| T08-b | 검수 전 비식별본을 L2로 | PASS | tests/acceptance/test_t09_deid_review.py::test_t09_deid_review_flow<br>tests/unit/test_sharing_rules.py::test_t08_b_unreviewed_deid_cannot_reach_l2 |
| T08-c | 무작위 속성 조합과 파생 관계 (원본 → 비식별본 → 데이터셋 사본) | PASS | tests/unit/test_sharing_rules.py::test_t08_c_no_path_breaks_domain_rules |
| T08-d | 제공기관 2단 약정 없음·만료 | PASS | tests/integration/test_sharing.py::test_sharing_flow<br>tests/unit/test_sharing_rules.py::test_t08_d_agreement_required |
| T08-e | 제공기관 한 곳이 거절 | PASS | tests/acceptance/test_t08_t15_sharing.py::test_t08_t15_dataset_distribution |
| T08-f | 약정 허용 목적 밖의 이용 신청 | PASS | tests/acceptance/test_t08_t15_sharing.py::test_t08_t15_dataset_distribution |
| T08-g | 저장된 자산 상태 | PASS | tests/unit/test_sharing_rules.py::test_t08_c_no_path_breaks_domain_rules |
| T08-h | 원본(variant=ORIGINAL)이나 CORE 원본의 파생본을 L2로 | PASS | tests/integration/test_sharing.py::test_sharing_flow<br>tests/unit/test_sharing_rules.py::test_t08_h_original_and_core_original_derivatives |
| T08-i | 운영자 최종 승인(share_status=APPROVED) 전에 파생본을 L2로 | PASS | tests/unit/test_sharing_rules.py::test_t08_i_operator_final_approval_required |
| T08-j | ORG_ADMIN이 아닌 역할의 L1 이상 결정 | PASS | tests/integration/test_sharing.py::test_sharing_flow<br>tests/unit/test_sharing_rules.py::test_t08_j_l1_needs_org_admin |
| T09-a | 모델 처리만 끝난 비식별본 | PASS | tests/acceptance/test_t09_deid_review.py::test_t09_deid_review_flow |
| T09-b | 검수 표본에서 누락 발견 | PASS | tests/acceptance/test_t09_deid_review.py::test_t09_deid_review_flow |
| T09-c | 누락률 보고 | PASS | tests/acceptance/test_t09_deid_review.py::test_t09_deid_review_flow |
| T10-a | `make restore-test` | PASS | tests/acceptance/test_t10_backup_restore.py::test_t10_tiered_backup_and_restore |
| T10-b | 백업 대상 확인 | PASS | tests/acceptance/test_t10_backup_restore.py::test_t10_tiered_backup_and_restore |
| T11-a | 템플릿으로 초안 | PASS | tests/acceptance/test_t11_reports.py::test_t11_report_flow_and_t06a_trace |
| T11-b | 데이터 없는 지표 | PASS | tests/acceptance/test_t11_reports.py::test_t11_report_flow_and_t06a_trace |
| T11-c | 정답 없이 모델 결과만으로 계산한 지표 | PASS | tests/acceptance/test_t11_reports.py::test_t11_report_flow_and_t06a_trace |
| T11-d | 사용자가 자동값을 고침 | PASS | tests/acceptance/test_t11_reports.py::test_t11_report_flow_and_t06a_trace |
| T11-e | 인용 자산 격리 | PASS | tests/acceptance/test_t11_reports.py::test_t11_report_flow_and_t06a_trace |
| T11-f | 실제 관리기관 양식(HWPX 포함) | NEEDS_INPUT | 실제 관리기관 보고서 양식(HWPX) 확보 후 |
| T12-a | 동시 업로드 세션 생성 | PASS | tests/acceptance/test_t12_concurrency.py::test_t12a_concurrent_upload_sessions_never_exceed_quota |
| T12-b | 검색·업로드·관측값 부하 | NEEDS_HARDWARE | 부하 도구(loadtest/)는 있음. 수치는 운영 하드웨어에서 실측 |
| T12-c | 작업 큐 워커 정지 중 | PASS | tests/acceptance/test_t12_concurrency.py::test_t12c_ingest_continues_while_worker_stopped |
| T13-a | 외부 개발자가 contracts와 예제만으로 업로드·관측값·이벤트 전송 | NEEDS_INPUT | 외부 개발자 시험 (사람 참여) |
| T13-b | 사전 검증 API·기능 상태 API | PASS | tests/integration/test_catalog.py::test_validate_endpoint_uses_same_rules<br>tests/integration/test_system.py::test_capabilities_requires_auth_and_hides_settings |
| T14-a | Runbook대로 기동·백업·복원·자격증명 폐기·긴급 열람 | NEEDS_INPUT | 스테이징에서 Runbook 수행 기록 (docs/runbook/records/) |
| T14-b | 스토리지 장애·작업 큐 정지·디스크 임계 | NEEDS_INPUT | 스테이징에서 Runbook 수행 기록 |
| T15-a | 보존 만료 + 법적 보류 | PASS | tests/integration/test_lifecycle.py::test_dry_run_legal_hold_and_expiry_execution |
| T15-b | 삭제 요청 | PASS | tests/integration/test_lifecycle.py::test_asset_deletion_with_derivatives_certificate_and_reupload |
| T15-c | 유효한 이용 권한이 있는 데이터셋의 포함 자산 삭제 | PASS | tests/acceptance/test_t08_t15_sharing.py::test_t08_t15_dataset_distribution |
| T15-d | Grant 철회·만료 | PASS | tests/acceptance/test_t08_t15_sharing.py::test_t08_t15_dataset_distribution<br>tests/acceptance/test_t08_t15_sharing.py::test_t15_deadline_and_expiry |
| T15-e | v1 Grant로 v2 접근 | PASS | tests/acceptance/test_t08_t15_sharing.py::test_t08_t15_dataset_distribution |
| T15-f | 회차 미상(UNKNOWN_SESSION) 격리 자산이 30일 지남 | NOT_IMPLEMENTED |  |
| T15-g | ORG_ADMIN이 공유 등급을 PRIVATE로 낮춤 | PASS | tests/acceptance/test_t08_t15_sharing.py::test_t08_t15_dataset_distribution |
| T16-a | GPU 없음 | PASS | tests/acceptance/test_t09_deid_review.py::test_t09_deid_review_flow |
| T16-b | `make bench` | PASS | tests/acceptance/test_t12_concurrency.py::test_t16b_bench_reports_per_step_throughput |
| T16-c | 우선순위 | PASS | tests/integration/test_pipeline.py::test_queue_retry_lease_and_resource_classes |
| T17-a | 반경·시간 창 결합 | PASS | tests/acceptance/test_t17_env_join.py::test_t17_collect_join_backfill_and_alert |
| T17-b | 시간·해역이 맞지 않는 관측값 | PASS | tests/acceptance/test_t17_env_join.py::test_t17_collect_join_backfill_and_alert |
| T17-c | 재배포 불가 소스 | PASS | tests/acceptance/test_t17_env_join.py::test_t17_collect_join_backfill_and_alert |
| T19-a | NGSI-LD 어댑터 꺼짐 | NOT_IMPLEMENTED | 16번 NGSI-LD 어댑터 (조건부) |
| T19-b | 게이트 통과 후 적합성 | NOT_IMPLEMENTED | 16번 (조건부, 게이트 통과 후) |
