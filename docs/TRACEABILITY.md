# 요구사항 추적표

`tools/check_traceability.py --write`가 만든 파일이다. 직접 고치지 말고 스펙의 '추적' 절을 고친 뒤 다시 만든다.

## 1. 기준 문서 기능 ID → 스펙
| 기능 ID | 기능 | 우선 | 스펙 |
|---|---|---|---|
| STD-01 | 공통 메타데이터·유형별 스키마·코드표 관리 | P0 | 03-metadata-standard, 07-sensor-event-ingestion |
| STD-02 | 스키마 검증기, 시간·좌표·단위 정규화 | P0 | 03-metadata-standard, 07-sensor-event-ingestion |
| STD-03 | 품질 규칙·품질 리포트 | P0 기본 / P1 | 03-metadata-standard |
| ONB-01 | 데이터 연계 카드·온보딩 | P0 | 03-metadata-standard, 09-workspace-portal |
| ONB-02 | 기업·장비별 수집 정책 (collection_mode·data_role 기본값·쿼터·보존·시간대) | P0 | 03-metadata-standard, 09-workspace-portal |
| ING-01 | 폴더 동기화 에이전트 | P0 | 06-sync-agent |
| ING-02 | 웹 대용량 업로드 | P0 | 04-ingestion-api, 09-workspace-portal |
| ING-03 | REST/S3 업로드 세션·멀티파트·완료 | P0 | 04-ingestion-api |
| ING-04 | 체크섬·멱등성·영수증·악성코드 검사(대상 시) | P0 | 04-ingestion-api |
| ING-05 | 하트비트·에이전트 원격 상태 | P0 | 04-ingestion-api, 06-sync-agent |
| ING-06 | 선박 store-and-forward 엣지 | 앵커 P0 / P1 | 15-edge-collectors |
| ING-07 | CCTV 엣지(push)·NVR/VMS 커넥터 | 앵커 P0 / P1 | 15-edge-collectors |
| ING-08 | 센서 IoT 어댑터 | 대상 기업 P0 | 07-sensor-event-ingestion |
| ING-09 | 공공 Open API 수집기 | P0 (소스 1~2개) | 12-open-api-collector |
| ING-10 | 영상·회차 환경 정보 자동 결합 | P1 | 12-open-api-collector |
| ING-11 | 오프라인 디스크 이관 | P1 | 15-edge-collectors |
| ING-12 | CCTV 현장 비식별 옵션 | P1 (해당 기업) | 15-edge-collectors |
| STO-01 | 존 분리(원본·가공·데이터셋·격리)·기업별 버킷·암호화 | P0 | 08-storage-lifecycle |
| STO-02 | 쿼터·사용량 | P0 | 08-storage-lifecycle |
| STO-03 | 보존·수명주기 자동 실행 | P0 | 08-storage-lifecycle |
| STO-04 | Hot/Warm 계층 | P0 | 08-storage-lifecycle |
| STO-05 | 계층형 백업·복원 | P0 | 08-storage-lifecycle |
| STO-06 | 삭제 요청 처리·삭제 기록 | P1 | 08-storage-lifecycle |
| DB-01 | 메타·업무 관계형 DB | P0 | 03-metadata-standard |
| DB-02 | 시계열·공간 확장 | P0 | 03-metadata-standard, 07-sensor-event-ingestion |
| DB-03 | 벡터(장면 검색) 확장 | P1 | 03-metadata-standard, 14-deidentification-tagging |
| PRC-01 | 비동기 파이프라인·재시도·격리·계보 | P0 | 05-processing-pipeline |
| PRC-02 | 메타 추출·프록시·썸네일 | P0 | 05-processing-pipeline |
| PRC-03 | 무결성 로그(추가 전용) | P0 | 05-processing-pipeline |
| PRC-04 | GPU 작업 스케줄러 | P0 | 05-processing-pipeline |
| PRC-05 | 선별 비식별 | P1 | 14-deidentification-tagging |
| PRC-06 | 비식별 검수 도구 | P1 | 14-deidentification-tagging |
| PRC-07 | 샘플링 태깅·이벤트 클립 | P1 | 14-deidentification-tagging |
| PRC-08 | 장면 임베딩 | P1 | 14-deidentification-tagging |
| PRC-09 | 라벨 초안 | P1~P2 | — (라벨 초안 (P1~P2) — 라벨링 도구 연계 후 별도 스펙) |
| PRC-10 | 재처리 | P1 | 05-processing-pipeline |
| CAT-01 | 계층·조건 검색, 기본 뷰어 | P0 | 09-workspace-portal |
| CAT-02 | 태그·장면·지도 검색 | P1 | 14-deidentification-tagging |
| CAT-03 | 데이터셋 카탈로그·데이터 카드 | 자료구조 P0 / 서비스 P1 | 13-sharing-governance |
| POR-01 | 기업 대시보드 | P0 | 09-workspace-portal |
| POR-02 | 실증회차·이벤트·증빙 관리 | P0 | 07-sensor-event-ingestion, 09-workspace-portal, 10-evidence-reports |
| POR-03 | 보고서 초안 (PDF·Excel·HWP/HWPX) | P0 기본형 | 10-evidence-reports |
| POR-04 | 성능분석 (정답·조건·모델 버전 연결) | P1 | 10-evidence-reports |
| POR-05 | 공유 설정·이용 이력 | P1 | 13-sharing-governance |
| ADM-01 | 관리기관 보드 (원본 비노출) | P0 | 09-workspace-portal |
| ADM-02 | 운영자 콘솔 (사용자·과제·정책·처리 현황) | P0 | 02-identity-tenancy, 09-workspace-portal |
| ADM-03 | 데이터셋 구성·공유 최종 승인 | P1 | 13-sharing-governance |
| API-01 | API 게이트웨이 | P0 | 04-ingestion-api |
| API-02 | OpenAPI 문서·샌드박스·SDK 예제 | P0 | 01-dev-foundation, 03-metadata-standard, 04-ingestion-api, 17-acceptance-release |
| API-03 | 웹훅 | P0 | 04-ingestion-api |
| IAM-01 | SSO·MFA·정책 인가·키·기기 인증 | P0 | 02-identity-tenancy |
| SEC-01 | 감사 로그·운영자 긴급 열람 절차 | P0 | 02-identity-tenancy |
| SEC-02 | 망 구간 분리 | P0 | — (망 구간 분리 — 운영 배포 스펙(하드웨어 후)에서) |
| SEC-03 | 공유 가능 조건 강제 (3.3) | P0 | 13-sharing-governance |
| SHR-01 | 이용 신청·다단계 승인·기한부 권한 | P1 | 13-sharing-governance |
| SHR-02 | 기한부 다운로드·API 배포·철회 통지 | P1 | 13-sharing-governance |
| OPS-01 | 수집 정상성 모니터링 | P0 | 11-collection-monitoring |
| OPS-02 | 시스템·하드웨어 감시, 용량 예측 | P0 | 08-storage-lifecycle, 11-collection-monitoring |
| OPS-03 | 정기 점검 기록 | P0 | 11-collection-monitoring, 17-acceptance-release |
| OPS-04 | 기업별 자원 사용 리포트 | P1 | 08-storage-lifecycle |
| INT-01 | NGSI-LD 어댑터 | 조건부 | 16-ngsi-ld-adapter |
| INT-02 | 광역 허브 연계 | 조건부 | 16-ngsi-ld-adapter |
| EXT-01 | 격리 탐색 환경 | P2 | — (P2 — 수요·비용 검증 후 별도 스펙) |
| EXT-02 | 크레딧·거래 | 보류 | — (보류) |
| EXT-03 | 대형 GPU 학습 환경 | 제외 | — (제외) |
| EXT-04 | 웹 크롤러 | 제외 | — (제외 (크롤러 대신 12번 Open API 수집)) |

## 2. 수용시험 → 스펙
| ID | 주제 | 스펙 |
|---|---|---|
| T01 | 테넌트 격리 | 02-identity-tenancy, 04-ingestion-api, 09-workspace-portal, 13-sharing-governance, 17-acceptance-release |
| T11 | 보고서 (실제 양식, HWPX 포함) | 10-evidence-reports |
| T02 | 자동수집 (폴더 에이전트) | 04-ingestion-api, 06-sync-agent, 09-workspace-portal, 15-edge-collectors |
| T12 | 성능·동시성·피크 | 04-ingestion-api, 05-processing-pipeline, 08-storage-lifecycle, 17-acceptance-release |
| T03 | 대용량 재개 | 01-dev-foundation, 04-ingestion-api, 06-sync-agent, 08-storage-lifecycle, 15-edge-collectors |
| T13 | API 문서만으로 외부 연계 | 01-dev-foundation, 03-metadata-standard, 04-ingestion-api, 07-sensor-event-ingestion |
| T04 | 무결성·격리 | 04-ingestion-api, 05-processing-pipeline |
| T14 | 운영 인수 (Runbook) | 17-acceptance-release |
| T05 | 스키마·정규화 (촬영시각 미상 포함) | 01-dev-foundation, 03-metadata-standard, 04-ingestion-api, 06-sync-agent, 07-sensor-event-ingestion |
| T15 | 보존·삭제·철회 | 04-ingestion-api, 08-storage-lifecycle, 13-sharing-governance |
| T06 | 계보 역추적 | 03-metadata-standard, 05-processing-pipeline, 07-sensor-event-ingestion, 08-storage-lifecycle, 10-evidence-reports, 13-sharing-governance, 14-deidentification-tagging, 17-acceptance-release |
| T16 | GPU/CPU 처리량·우선순위 | 05-processing-pipeline, 14-deidentification-tagging |
| T07 | 수집 정상성 경보 | 06-sync-agent, 07-sensor-event-ingestion, 09-workspace-portal, 11-collection-monitoring |
| T17 | 환경 정보 결합 | 12-open-api-collector |
| T08 | 공유 조건 강제 | 13-sharing-governance |
| T18 | 확장성 (노드·디스크 추가) — 하드웨어 후 | 17-acceptance-release |
| T09 | 비식별 검수 절차 | 14-deidentification-tagging |
| T19 | 조건부 표준 연계 (NGSI-LD·광역 허브) | 16-ngsi-ld-adapter |
| T10 | 백업·복원 | 08-storage-lifecycle |
| T20 | 구축비·운영비 구분 — 문서 시험 | — (사업 문서 시험 (SW 범위 밖)) |

## 3. 스펙별 연결
| 스펙 | 기능 ID | 수용시험 |
|---|---|---|
| 01-dev-foundation | API-02 | T03, T05, T13 |
| 02-identity-tenancy | ADM-02, IAM-01, SEC-01 | T01 |
| 03-metadata-standard | API-02, DB-01, DB-02, DB-03, ONB-01, ONB-02, STD-01, STD-02, STD-03 | T05, T06, T13 |
| 04-ingestion-api | API-01, API-02, API-03, ING-02, ING-03, ING-04, ING-05 | T01, T02, T03, T04, T05, T12, T13, T15 |
| 05-processing-pipeline | PRC-01, PRC-02, PRC-03, PRC-04, PRC-10 | T04, T06, T12, T16 |
| 06-sync-agent | ING-01, ING-05 | T02, T03, T05, T07 |
| 07-sensor-event-ingestion | DB-02, ING-08, POR-02, STD-01, STD-02 | T05, T06, T07, T13 |
| 08-storage-lifecycle | OPS-02, OPS-04, STO-01, STO-02, STO-03, STO-04, STO-05, STO-06 | T03, T06, T10, T12, T15 |
| 09-workspace-portal | ADM-01, ADM-02, CAT-01, ING-02, ONB-01, ONB-02, POR-01, POR-02 | T01, T02, T07 |
| 10-evidence-reports | POR-02, POR-03, POR-04 | T06, T11 |
| 11-collection-monitoring | OPS-01, OPS-02, OPS-03 | T07 |
| 12-open-api-collector | ING-09, ING-10 | T17 |
| 13-sharing-governance | ADM-03, CAT-03, POR-05, SEC-03, SHR-01, SHR-02 | T01, T06, T08, T15 |
| 14-deidentification-tagging | CAT-02, DB-03, PRC-05, PRC-06, PRC-07, PRC-08 | T06, T09, T16 |
| 15-edge-collectors | ING-06, ING-07, ING-11, ING-12 | T02, T03 |
| 16-ngsi-ld-adapter | INT-01, INT-02 | T19 |
| 17-acceptance-release | API-02, OPS-03 | T01, T06, T12, T14, T18 |
