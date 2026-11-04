# 설계: 13 공유 등급·데이터셋·승인형 배포

## 이번에 구현한 것 (마이그레이션 0010, 0011)
| 구성 | 구현 |
|---|---|
| Sharing_Rules | `domain/sharing_rules.py` 순수 함수 (domain.md 4장). L1=ORG_ADMIN 승인, L2=파생본·BYPRODUCT(조상 포함)·비식별 상태·검수 합격·운영자 최종 승인(share_status=APPROVED)·SECURITY 아님·legal_basis_ref·2단 약정 유효·ORG_ADMIN, L3=L2+데이터셋 포함+운영자, L4=L2+가명 제외+INTERNAL+운영자. 파생본은 조상의 가장 엄격한 data_role·sensitivity를 따르고 CORE 원본의 파생본은 L1을 넘지 않는다. 데이터셋 등급=항목 최저 |
| 우회 방지 | SQLAlchemy `before_flush` 가드: sharing_scope 상향은 sharing 서비스가 표시한 변경만 저장 (`SharingBypass`). 새 자산은 L1까지 (ADR-0019) |
| 공유 후보 | `POST /v1/share-candidates`(자산 목록 또는 회차): BYPRODUCT 원본만 CANDIDATE, CORE·파생본·상태 불일치는 사유와 함께 제외 |
| data_role | 기존 `PATCH /v1/assets/{id}`(ORG_ADMIN·사유): CORE로 바꾸면 자신과 파생본을 L1 이하로 내리고(기존 권한 즉시 철회), 원본 공유 심사를 철회 |
| 심사 흐름 | `POST /v1/assets/{id}/share-review` action: START_LEGAL_REVIEW·PASS_LEGAL_REVIEW(legal_basis_ref 필수, 비식별 작업 `deidentify` 등록 → 14번)·APPROVE(검수 합격 파생본 필수, 파생본에 APPROVED·legal_basis_ref 기록)·REQUEST_REPROCESS·REJECT(사유 필수, 기관 알림)·RESUBMIT·WITHDRAW(파생본 L1 이하). 전이는 domain.md 5장 표, 역할이 다르면 403. 이력 `share_review_events`. PROCESSING → QUALITY_REVIEW는 SERVICE(`sharing.mark_processed`, 개발용 CLI `share-processed`) |
| 분류 지정 | `PATCH /v1/assets/{id}/classification`(운영자): sensitivity_class, privacy_status=NOT_APPLICABLE을 근거(점검 기록 참조)·사유와 함께, 감사 |
| 등급 변경 | `POST /v1/assets/{id}/share-requests`(ORG_ADMIN만, 그 밖 403): 상향은 규칙 평가 → 미충족 422 목록, 운영자 승인만 남으면 IN_REVIEW → `POST /v1/share-requests/{id}/decisions`(운영자, 결정 시점 재평가). 하향은 언제든, existing_grant_handling 필수(REVOKE_NOW면 그 자산이 든 버전의 권한 철회). 이력 GET |
| 데이터셋 | `POST /v1/datasets`, `POST /v1/datasets/{id}/versions`(운영자): 항목은 지금 L2 조건을 충족하는 공유용 파생본만, DATASET 존에 사본(경로 `variant/asset_id.ext`, 원래 파일명 없음), 매니페스트(계약 검사·sha256·DATASET 존 저장). 버전 번호 중복 409(불변). `publish`는 최신 버전 데이터 카드 검사 |
| 카탈로그 | CONSUMER: 게시된 데이터셋 중 버전 등급 L3 이상만, 데이터 카드·통계(자산 목록 없음). 제공기관: 자기 자산이 든 것, 운영자: 전체 |
| 이용 신청 | `POST /v1/access-requests`(CONSUMER, 약정 동의): 데이터 카드 허용 목적 + 모든 제공기관 2단 약정 허용 목적(위반 기관 수만), 응답 기한 `ACCESS_DECISION_DAYS`(14일). 결정은 기관별 1회: 제공기관 모두 승인 → PROVIDER_APPROVED → 운영자 승인 → GRANTED + Grant(신청 기간). 한 곳 거절 = DENIED(운영자가 못 뒤집음). 기한 초과·기간 만료는 워커 정비 주기(`datasets.expire`) |
| 배포 | `POST /v1/distributions`(CONSUMER, 유효 Grant만, 아니면 410): 파일별 사전 서명 URL(`PRESIGN_DOWNLOAD_TTL_S`, 기본 5분) + 받는 쪽 매니페스트(`RECIPIENT_MANIFEST.json`: 배포·권한·기관·요청자). URL은 기록하지 않고 감사·이용 이력에 남김 |
| 철회·파기 | `POST /v1/grants/{id}/revoke`(제공기관 ORG_ADMIN·운영자): 즉시 발급 중지, 이용자 알림에 "이미 받은 파일은 회수되지 않음", `destruction-confirmation`(이용기관 1회) |
| 이용 이력 | `GET /v1/usage-history`(제공기관): 신청·결정·권한·다운로드·철회/만료, 이용기관 이름·목적 |
| DCAT | `GET /v1/catalog/dcat`(인증 없음): 모든 항목이 L4인 게시 데이터셋만 DCAT 3 JSON-LD(dcat:Catalog/Dataset, dct:title·identifier·issued·temporal·spatial(일반화 해역)·rightsHolder) |
| 화면 | 자산 상세 '공유' 카드(후보 지정·심사 이력·등급 변경 신청과 미충족 조건 표시), 운영 콘솔 '공유·데이터셋'(심사 처리·데이터셋 만들기·버전(데이터 카드 JSON)·게시·최종 승인), 이용기관 '데이터셋 카탈로그'(신청·권한·다운로드·파기 확인), 제공기관 '이용 신청'(승인·거절·철회·이용 이력) |

## 결정
- ADR-0019: 판단은 순수 함수 하나, 상향 우회는 ORM 가드로 차단, 데이터셋 항목 표, 버전 등급은 현재 항목 등급으로 계산, 버전 생성 동기(계약 202 → 201)

## 확인 필요 / 다음 스펙과 연결
- 비식별·검수(14번): `deidentify` 작업 처리, PROCESSING → QUALITY_REVIEW, 파생본 review_result, REVIEWER의 범위(지금 REVIEWER는 자산을 조회할 범위 규칙이 없다)
- 약정 등록 API(03번): 지금은 관리 명령 `python -m app.cli agreement <기관> --reference … --purposes AI_TRAINING,…`
- 대용량 데이터셋 버전 생성은 작업 큐로 (항목 1000개 초과)
- 카탈로그 샘플 썸네일: 승인된 THUMBNAIL 파생본을 데이터 카드와 함께 보여 주는 방식은 14번 이후
- 광역 데이터 허브 연계 프로파일(DCAT-AP 등) 확인 필요 — 지금은 DCAT 3 기본 어휘만
- 데이터 카드 항목은 기준 문서 18.2장을 옮긴 계약(`dataset-version-manifest.schema.json`)을 따른다
