# 해양 규제자유특구 데이터 플랫폼 — GitHub 오픈소스 59건 도입 검토

> 파일: `gptv0.07_opensource_review.md`  
> 작성 기준: 2026-11-04  
> 검토 대상: 사용자가 제공한 `해양실증데이터플랫폼_오픈소스후보_17개Spec_59건_20261104(1).xlsx`의 4개 시트, 기존 Kiro `v0.06`의 `.kiro/steering/tech.md`, README, START_HERE.  
> 성격: **기술 적합성 및 라이선스 1차 실무 검토.** 배포 버전과 전이 의존성에 대한 최종 법률 검토·보안 감사·PoC가 완료된 것은 아님.

## 0. 핵심 판단

이 후보 목록은 기능별 후보 도출과 사전 PoC 범위가 잘 정리된 실용적인 자료다. 다만 **59건을 59개 컴포넌트로 도입하지 않는다.** 59건은 58개 고유 GitHub 저장소(OPA 중복)이며, 동일 역할의 대안이 다수 포함된다. 오픈소스가 해결하는 것은 일반적인 인증·업로드·영상 처리·검증 기능이고, **기관별 권한·실증 데이터 모델·증빙 계보·제3자 활용 승인·데이터 계약**은 여전히 자체 구현해야 한다.

### 최종 1차 분류

| 분류 | 매핑 건수 | 의미 |
|---|---:|---|
| 채택 | 14 | 기존 Kiro v0.06과 호환성이 높아 기본 구성·개발 도구로 사용하되 버전/라이선스 고정 |
| PoC | 8 | 테스트 후 도입 여부 결정 |
| 현장PoC | 3 | 실제 선박/CCTV/센서 장비·권한이 확인된 뒤 평가 |
| 조건부 | 5 | 요구사항 또는 배포 조건 충족 시만 |
| 대안 | 3 | 다른 후보가 부적합하면 교체 후보 |
| 보류 | 17 | 초기 단순성과 운영비 관점에서 미도입 |
| 후속 | 7 | 확장 요건이 발생하면 검토 |
| 제외 | 1 | 초기 기본 스택과 충돌하거나 불필요 |
| 중복제거 | 1 | 동일 프로젝트 재기재 |
| **합계** | **59** | 중복을 감안한 고유 저장소 58개 |

**중요:** 여기서 '채택'은 라이선스 안전이 공식 확정됐다는 뜻이 아니라, 설계에 반영해 구현을 진행할 가치가 높다는 뜻이다.

## 1. 기존 Kiro v0.06과 충돌하는 8가지 사항

| 번호 | 스프레드시트 제안 | v0.06 기본 설계 | 합의할 방향 |
|---|---|---|---|
| C01 | Celery | PostgreSQL `FOR UPDATE SKIP LOCKED` 큐 | **후자 유지**. 부하 측정 전 이중 큐 금지 |
| C02 | rclone + watchdog | **Go 에이전트 + SQLite 저널** | Go 에이전트 유지. rclone은 초기 이관/보조 진단에 한정 |
| C03 | tusd + Uppy | **S3 presigned multipart + boto3** | Uppy `@uppy/aws-s3`부터 PoC. tusd는 tus 프로토콜이 꼭 필요할 때만 |
| C04 | MUI, Refine, React-admin | **React + Ant Design (ko_KR)** | 디자인 시스템은 하나만. MUI 추가는 별도 전체 전환 결정 시만 |
| C05 | OPA·OpenFGA·Casbin | **자체 PolicyPort + 데이터셋 승인 상태기계** | 승인 상태·최종 DB 인가 판정은 자체. 외부 정책 엔진은 후속 |
| C06 | OpenMetadata·DataHub·Meilisearch·CKAN | **PostgreSQL 기본 검색 + 승인형 카탈로그** | P0에서 전문 검색/카탈로그 풀스택 미설치 |
| C07 | SeaweedFS | **개발용 S3 gateway, 운영 제품 미정** | 개발/테스트 유지, PB 운영 제품 채택은 별도 PoC와 장애복구 검증 |
| C08 | Grafana | **Prometheus + 선택형 Grafana** | 독립 모니터링 서비스이면 조건부. 제품 코드 임베딩과 별도 검토 |

### 업로드 프로토콜 채택 결정

- **브라우저 업로드:** Uppy + `@uppy/aws-s3`를 우선 PoC한다. S3 호환 객체저장소로 직접 보내고, 메타데이터/업로드 권한·완료 검증은 자사 API가 처리한다.
- **현장 자동수집:** Go 단일 실행파일 에이전트 + SQLite 상태저널 + 서명 URL 갱신·재전송. rclone은 마이그레이션/복구 유틸로만 PoC한다.
- **tusd:** 독자적인 tus 프로토콜을 지원해야 하는 외부 클라이언트가 확인되거나 S3 멀티파트가 사용자 테스트에 불합격한 경우에만 고려한다.
- **메타데이터만 수집(C0):** Blob 생성·hash·파일 용량 강제 금지. 스키마/API는 별도로 유지한다.

### 객체저장소 도입 결정

- SeaweedFS 공개 저장소는 Apache-2.0으로 공개되어 있으나 **Enterprise와 기능 차이**가 있다. 특히 운영상 핵심인 복구·자가치유·일부 EC 복구기능 등을 OSS에 있다고 가정하지 않는다.
- 개발 S3 어댑터는 SeaweedFS 기반으로 시작할 수 있지만 **PB 운영환경은 벤치마크/노드 장애·디스크 손상·복원/재동기화 시험·기술지원 조건**을 충족해야 선정한다.
- 애플리케이션은 S3 StoragePort 인터페이스 외 기능에 의존하지 않는다. 제품 고유 기능이 필요하면 새 어댑터로 격리한다.

## 2. 라이선스 검토: 피해야 할 오해

1. **오픈소스는 '무조건 라이선스 문제가 없는 무료 코드'가 아니다.** MIT/BSD/Apache도 저작권·고지 보존이 필요하고, Apache-2.0은 관련 NOTICE·변경 고지·특허 조항 검토가 필요하다.
2. **FFmpeg:** 기본은 LGPL 계열이지만 컴파일 플래그·외부 코덱에 따라 GPL이 되거나 `--enable-nonfree`에서는 재배포가 불가능해질 수 있다. API에 직접 링크하지 않고 CLI 독립 프로세스로 써도 바이너리 납품·고지 문제는 따로 남는다. 실제 배포 바이너리의 `ffmpeg -buildconf`와 의존 코덱을 보존한다.
3. **Grafana/CKAN:** AGPL 계열을 수정·재배포·서비스에 통합하는 경우의 의무가 중요하다. **AGPL을 사용한다는 이유만으로 모든 자체 소스가 자동 공개되는 것은 아니지만**, 결합/수정/네트워크 제공 방식을 따져야 한다. 초기 제품 기능에는 복사해 넣지 않는다.
4. **Meilisearch:** CE MIT과 EE BUSL/상용 기능 구분. GitHub 저장소가 MIT과 BUSL을 함께 포함하므로 **CE 기능만 사용하는지** 확인한다.
5. **CVAT:** Community 코드는 MIT이나 FFmpeg 등의 부속 의존성과 온라인/Enterprise 이용조건은 별도다.
6. **authentik:** 기본 MIT, Enterprise 디렉터리/기능 별도 조건.
7. **ERDDAP:** GitHub 저장소에 CC0 표시가 있으나 공식 설명은 'Apache and Apache-like' 및 다수 구성요소를 언급한다. 서버 전체에 대한 외부 납품 조건은 별도 검토한다.
8. **SeaweedFS:** Apache 공개 코어와 별도 운영 라이선스가 필요한 Enterprise를 구분한다.
9. **영상·데이터·AI 모델 가중치의 라이선스는 코드의 오픈소스 라이선스와 독립적이다.** 특히 CCTV 원본 적법한 수집/제공, 선박 촬영 권리, 학습데이터 사용허락과 AI 모델 가중치 조건은 별도로 검토한다.

**검토 상태:** FFmpeg, Meilisearch, authentik, SeaweedFS, CVAT, CKAN, Grafana, ERDDAP 등 쟁점 후보는 공개된 공식 설명을 조사했다. **나머지 후보의 정확한 배포 태그·전이 의존성 전체를 개별 법률 실사한 것은 아니다.**

## 3. 추천 최소 오픈소스 조합 — 초기 P0

| 역할 | 선택안 | 왜 필요한가 | 자체 구현 부분 |
|---|---|---|---|
| 백엔드 | FastAPI · SQLAlchemy · Alembic · python-jsonschema | REST, DB, 계약 검증 | 조직 격리/실증업무 로직 |
| 로그인 | Keycloak | OIDC·SSO·사용자 인증 | 기관별 자산·데이터셋 업무 인가 |
| 데이터베이스 | PostgreSQL + PostGIS (기존 설계) | 정형·위치 메타데이터 | Blob/Asset/DerivedAsset/Dataset 관계 |
| S3 연계 | boto3 · SeaweedFS(개발용) | HW 독립 객체 저장소 | 업로드 승인/무결성/수명주기 |
| 웹 UI | React · Ant Design(기존) · TanStack Query · React Hook Form | 기업·기관 포털 | 실증·공유·승인 UX |
| 브라우저 업로드 | Uppy `@uppy/aws-s3` (PoC) | 이어올리기·분할전송 | 업로드 세션 상태·보안·Quota |
| 현장 자동수집 | **자체 Go 에이전트**, rclone 보조 PoC | 자동수집/오프라인 전송 | 정책·저널·재전송·heartbeat |
| 영상 처리 | FFmpeg CLI(빌드 검증), OpenCV(필요시) | 프록시/클립/썸네일 | 비식별 품질검수·가공계보 |
| 보고서 | WeasyPrint(PDF), 실제 HWPX·DOCX 양식 추후 판단 | 초안·내보내기 | 지표 근거·승인·템플릿 |
| 수집 관측·복구 | Prometheus, restic(복원 PoC) | 연결상태·경보·중요 자료 백업 | 수집 누락 판정·복원 정책 |
| 품질·보안 | pytest · Schemathesis · Playwright · Trivy · Syft · ZAP | 계약·BOLA·SBOM·취약점 검증 | 해양 특구 수용시험 시나리오 |

**초기에는 추가하지 않을 것:** Kafka·Celery·tusd·DataHub·OpenMetadata·CKAN·별도 검색엔진·분산 추적 플랫폼·범용 AI 학습 클러스터·NGSI-LD 전체 코어.

## 4. 17개 Spec / 59건별 검토 표

표의 '라이선스(원표)'는 **첨부 엑셀의 기재값**으로, 전 후보 버전별 확정 검증 결과가 아니다. URL은 원표에서 가져왔다.

### Spec 01 — 플랫폼 기초

| 후보 | 원표 라이선스 | 도입 판단 | 이유 및 PoC 포인트 |
|---|---|---|---|
| [FastAPI](https://github.com/fastapi/fastapi) | MIT | **채택** | FastAPI API 서버 기본 스택 유지. 새 웹 프레임워크 추가 불필요. |
| [SQLAlchemy](https://github.com/sqlalchemy/sqlalchemy) | MIT | **채택** | PostgreSQL ORM 및 이행(Alembic) 기본. 모든 데이터 조회에서 organization_id 정책 테스트. |
| [Celery](https://github.com/celery/celery) | BSD-3-Clause | **보류** | v0.06 PostgreSQL SKIP LOCKED 작업 큐 우선. 처리량 한계가 측정되면 Celery/브로커 비교. |

### Spec 02 — 인증·테넌시

| 후보 | 원표 라이선스 | 도입 판단 | 이유 및 PoC 포인트 |
|---|---|---|---|
| [Keycloak](https://github.com/keycloak/keycloak) | Apache-2.0 | **채택** | OIDC/SSO IdP 단일 후보. Keycloak 인증은 외주, 업무 인가는 플랫폼 자체 구현. |
| [Casdoor](https://github.com/casdoor/casdoor) | Apache-2.0 | **대안** | Keycloak 불합격 시 교체 후보. IdP 2개 동시 도입하지 않음. |
| [authentik Core](https://github.com/goauthentik/authentik) | MIT (Core, 유료기능 별도) | **대안** | Enterprise 기능 혼합 및 운영 부담 비교. Keycloak 대안일 뿐 동시 도입 금지. |

### Spec 03 — 실증·원천 관리

| 후보 | 원표 라이선스 | 도입 판단 | 이유 및 PoC 포인트 |
|---|---|---|---|
| [Refine](https://github.com/refinedev/refine) | MIT (Core) | **조건부** | Refine은 CRUD 행정 UI 생산성 PoC 후보. 기존 Ant Design/기존 폼 구조와 충돌 검토. |
| [React-admin](https://github.com/marmelab/react-admin) | MIT (OSS, Enterprise 별도) | **보류** | Refine과 동시 도입하지 않음. P0 기업 워크스페이스를 관리자 CRUD로 대체하지 않음. |
| [JSON Forms](https://github.com/eclipsesource/jsonforms) | MIT | **조건부** | 장비별 동적 데이터 연계 카드/메타데이터 폼이 많을 때 도입. |

### Spec 04 — 데이터 표준·검증

| 후보 | 원표 라이선스 | 도입 판단 | 이유 및 PoC 포인트 |
|---|---|---|---|
| [Frictionless (frictionless-py)](https://github.com/frictionlessdata/frictionless-py) | MIT | **PoC** | CSV/XLSX 센서 표준 검증에 한해 도입. 플랫폼의 JSON Schema 단일 계약을 대체하지 않음. |
| [Pandera](https://github.com/unionai-oss/pandera) | MIT | **보류** | Frictionless + jsonschema로 부족한 데이터프레임 검증이 있을 때만. |
| [python-jsonschema](https://github.com/python-jsonschema/jsonschema) | MIT | **채택** | 스키마 자동·수동 검증과 계약 CI. 기존 packages/schemas를 유일 계약으로 유지. |

### Spec 05 — 자산 업로드

| 후보 | 원표 라이선스 | 도입 판단 | 이유 및 PoC 포인트 |
|---|---|---|---|
| [tusd](https://github.com/tus/tusd) | MIT | **보류** | S3 multipart 기본과 중복. 브라우저·현장 환경에서 tus 프로토콜 필요성이 입증될 때만. |
| [Uppy](https://github.com/transloadit/uppy) | MIT | **PoC** | @uppy/aws-s3로 브라우저→S3 사전 서명 멀티파트. tus 플러그인과 동시 도입 금지. |
| [SeaweedFS](https://github.com/seaweedfs/seaweedfs) | Apache-2.0 (OSS) | **PoC** | 개발 S3 어댑터로 이미 사용 예정. 운영 PB에 적용 시 장애복구/유지보수/유료 기능 구분 실측. |

### Spec 06 — 현장 자동 동기화

| 후보 | 원표 라이선스 | 도입 판단 | 이유 및 PoC 포인트 |
|---|---|---|---|
| [rclone](https://github.com/rclone/rclone) | MIT | **PoC** | 초기 대량 이관·진단·수동 복구에 유용. 선박 Go 에이전트의 상태 저널과 정책을 대체하지 않음. |
| [Syncthing](https://github.com/syncthing/syncthing) | MPL-2.0 | **보류** | P2P 동기화는 중앙 승인형 수집과 목적이 다름. 데이터 삭제 전파/권한 모델 위험. |
| [watchdog](https://github.com/gorakhargosh/watchdog) | Apache-2.0 | **보류** | Python 파일 감시 라이브러리로, v0.06의 Go 에이전트 본체에는 직접 맞지 않음. |

### Spec 07 — 선박·CCTV 커넥터

| 후보 | 원표 라이선스 | 도입 판단 | 이유 및 PoC 포인트 |
|---|---|---|---|
| [python-onvif-zeep](https://github.com/FalkTannhaeuser/python-onvif-zeep) | MIT | **현장PoC** | NVR/카메라 모델별 ONVIF 지원 능력을 검증. 녹화파일 export가 된다고 가정하지 않음. |
| [MediaMTX](https://github.com/bluenviron/mediamtx) | MIT (본체) | **현장PoC** | 승인된 스트림의 프록시/녹화 PoC 때만. 중앙 영상 무제한 중계는 제외. |
| [go2rtc](https://github.com/AlexxIT/go2rtc) | MIT | **대안** | MediaMTX와 한 개만 시험. 우선 실제 장비/권한 확인. |

### Spec 08 — 센서·이벤트

| 후보 | 원표 라이선스 | 도입 판단 | 이유 및 PoC 포인트 |
|---|---|---|---|
| [Telegraf](https://github.com/influxdata/telegraf) | MIT | **현장PoC** | MQTT·센서 기업이 실제 존재할 때 입력 플러그인 검증. 전체 IoT 플랫폼 구축 금지. |
| [Node-RED](https://github.com/node-red/node-red) | Apache-2.0 | **보류** | 다양한 센서 프로토콜 연계 작업이 증가할 때. 복잡한 상태 흐름의 운영 부담 고려. |
| [Eclipse Mosquitto](https://github.com/eclipse-mosquitto/mosquitto) | EPL-2.0 OR EDL-1.0 | **조건부** | MQTT 장비 사용 시만 브로커로 채택. 개인정보·보안 별도 설정. |
| [FROST Server](https://github.com/FraunhoferIOSB/FROST-Server) | LGPL-3.0-or-later | **후속** | SensorThings API가 외부 의무일 때만. 현재 센서 데이터 수집의 필수 요소 아님. |

### Spec 09 — 처리·비식별

| 후보 | 원표 라이선스 | 도입 판단 | 이유 및 PoC 포인트 |
|---|---|---|---|
| [FFmpeg](https://github.com/FFmpeg/FFmpeg) | LGPL-2.1+ 또는 GPL (빌드별) | **PoC** | 별도 실행파일로 영상 분석·프록시 제작. 배포 빌드 플래그/코덱의 LGPL·GPL·nonfree 검사. |
| [OpenCV](https://github.com/opencv/opencv) | Apache-2.0 (4.5+) | **PoC** | 영상 프레임 기반 처리 유틸. 얼굴·선박번호 탐지 모델 자체와 혼동 금지. |
| [CVAT Community](https://github.com/cvat-ai/cvat) | MIT (Community, 부속물 별도) | **후속** | 데이터셋 라벨 검수 업무가 실제 늘 때 독립 CVAT 연동. 완료 상태가 공유 승인 아님. |
| [Presidio](https://github.com/data-privacy-stack/presidio) | MIT | **보류** | 텍스트/문서 PII 탐지 중심. 선박·해변 얼굴 영상의 주 처리기로 오해하지 않음. |

### Spec 10 — 검색·카탈로그

| 후보 | 원표 라이선스 | 도입 판단 | 이유 및 PoC 포인트 |
|---|---|---|---|
| [OpenMetadata](https://github.com/open-metadata/OpenMetadata) | Apache-2.0 (OSS) | **보류** | P0 단순 카탈로그에 운영 부담 과다. 연결 대상/규모 증가 시 비교. |
| [DataHub](https://github.com/datahub-project/datahub) | Apache-2.0 (OSS) | **보류** | OpenMetadata와 택1. 초기에 메타데이터 제품 2개 모두 설치하지 않음. |
| [Meilisearch CE](https://github.com/meilisearch/meilisearch) | MIT (CE); EE BUSL/상용 | **보류** | PostgreSQL 검색 먼저. 고급 검색 요구/성능 입증 시 CE만 사용·EE 기능 차단. |
| [CKAN](https://github.com/ckan/ckan) | AGPL-3.0-or-later | **제외** | 초기 승인형 카탈로그에 공공포털 풀스택·AGPL 복잡도 과다. |

### Spec 11 — 실증 증빙·보고

| 후보 | 원표 라이선스 | 도입 판단 | 이유 및 PoC 포인트 |
|---|---|---|---|
| [WeasyPrint](https://github.com/Kozea/WeasyPrint) | BSD-3-Clause | **PoC** | 실증 보고서 PDF 렌더러. 한글 폰트·페이지/표 편집·실제 양식 검수. |
| [docxtpl](https://github.com/elapouya/python-docx-template) | LGPL-2.1-only | **조건부** | DOCX 템플릿 요구 시 검토. LGPL 의무 및 HWPX 별도 구현 필요. |
| [Apache Superset](https://github.com/apache/superset) | Apache-2.0 | **보류** | 독립 BI 포털이 필요해지기 전은 기존 React 관리화면/SQL 지표 충분. |

### Spec 12 — 데이터셋·승인

| 후보 | 원표 라이선스 | 도입 판단 | 이유 및 PoC 포인트 |
|---|---|---|---|
| [Open Policy Agent](https://github.com/open-policy-agent/opa) | Apache-2.0 | **보류** | 규칙 엔진 정책이 복잡해지고 외부 일관성 요구가 생길 때. 현재 도메인 내 PolicyPort 우선. |
| [OpenFGA](https://github.com/openfga/openfga) | Apache-2.0 | **보류** | 관계 기반 세밀한 인가가 필요한 규모에 도달하면 OPA와 비교. |
| [Casbin](https://github.com/casbin/casbin) | Apache-2.0 | **보류** | OPA/OpenFGA/Casbin 중 하나만. 초기 비즈니스 승인 상태 자체 구현 필수. |

### Spec 13 — 통제형 배포

| 후보 | 원표 라이선스 | 도입 판단 | 이유 및 PoC 포인트 |
|---|---|---|---|
| [boto3](https://github.com/boto/boto3) | Apache-2.0 | **채택** | S3 공통 StoragePort용 Python SDK. 로컬·운영 스토리지 교체 가능한 검증. |
| [Apache APISIX](https://github.com/apache/apisix) | Apache-2.0 | **후속** | 통합 API 게이트웨이 실제 운영 환경 확정 때 설치. 개발은 FastAPI 모듈만 사용. |
| [Open Policy Agent](https://github.com/open-policy-agent/opa) | Apache-2.0 | **중복제거** | 12번 Spec의 OPA와 동일 프로젝트·동일 라이선스; 별도 의존성 아님. |

### Spec 14 — 운영·보안

| 후보 | 원표 라이선스 | 도입 판단 | 이유 및 PoC 포인트 |
|---|---|---|---|
| [Prometheus](https://github.com/prometheus/prometheus) | Apache-2.0 | **채택** | 시스템/수집 에이전트 메트릭 수집. 사업 API 데이터와 분리. |
| [Grafana](https://github.com/grafana/grafana) | AGPL-3.0-only (기본) | **조건부** | 독립 모니터링 서비스로만 PoC. AGPL, 커스터마이징·임베딩/재배포 계획 검토. |
| [OpenTelemetry Collector](https://github.com/open-telemetry/opentelemetry-collector) | Apache-2.0 | **후속** | 분산 추적 필요 시. 처음부터 별도 수집 서비스 규모 확대 금지. |
| [restic](https://github.com/restic/restic) | BSD-2-Clause | **PoC** | DB/핵심 증빙 독립 백업·복원 시험. 원본 객체 저장소 전체 백업과 구별. |

### Spec 15 — 기업·기관 웹 UI

| 후보 | 원표 라이선스 | 도입 판단 | 이유 및 PoC 포인트 |
|---|---|---|---|
| [MUI Material UI](https://github.com/mui/material-ui) | MIT (Core) | **보류** | 현재 React+Ant Design를 기본으로 선택했으므로 MUI 추가 시 UI 체계 충돌. 전체 전환 결정 없는 한 제외. |
| [TanStack Query](https://github.com/TanStack/query) | MIT | **채택** | React 서버 상태/캐시 관리 기존 스택과 일치. |
| [React Hook Form](https://github.com/react-hook-form/react-hook-form) | MIT | **채택** | 양식/검증 UI에 활용 가능. 기존 폼 구현과 일관된 방식으로 사용. |

### Spec 16 — 외부 표준 연계

| 후보 | 원표 라이선스 | 도입 판단 | 이유 및 PoC 포인트 |
|---|---|---|---|
| [pygeoapi](https://github.com/geopython/pygeoapi) | MIT | **후속** | OGC Features 등 실제 외부 제공 요구 있을 때. |
| [stac-fastapi](https://github.com/stac-utils/stac-fastapi) | MIT | **후속** | STAC 호환 데이터셋 외부 공개·API 요구가 있을 때. |
| [ERDDAP](https://github.com/ERDDAP/erddap) | 혼합/확인 필요 (저장소 CC0 표시) | **보류** | 해양 과학계열 대규모 배포 서버가 필요할 때 검토. 저장소 CC0 표시만으로 배포 전체 권리 확정 불가. |
| [RDFLib](https://github.com/RDFLib/rdflib) | BSD-3-Clause | **후속** | DCAT/RDF/JSON-LD 외부 표준 매핑 요구 시. |

### Spec 17 — 품질·출시 검증

| 후보 | 원표 라이선스 | 도입 판단 | 이유 및 PoC 포인트 |
|---|---|---|---|
| [pytest](https://github.com/pytest-dev/pytest) | MIT | **채택** | 단위/통합/계약 보안 테스트의 표준 실행. |
| [Schemathesis](https://github.com/schemathesis/schemathesis) | MIT | **채택** | OpenAPI 계약 기반 API 퍼징·오류 처리 검증. |
| [Playwright](https://github.com/microsoft/playwright) | Apache-2.0 | **채택** | 기업/운영기관 UX의 핵심 E2E 자동화. |
| [Trivy](https://github.com/aquasecurity/trivy) | Apache-2.0 | **채택** | 이미지·패키지 취약점 점검을 CI에 포함. |
| [Syft](https://github.com/anchore/syft) | Apache-2.0 | **채택** | SBOM 생성 후 배포 릴리스에 첨부. 고지 의무 평가 자료. |
| [OWASP ZAP](https://github.com/zaproxy/zaproxy) | Apache-2.0 | **채택** | 분리된 테스트 환경에 DAST 수행, 운영 API 무차별 스캔 금지. |

## 5. Kiro v0.06에 반영할 구체적인 개발 작업

이 검토서는 현재 Kiro 패키지를 무작정 변경하라는 지시가 아니라 **개발 규칙을 확정하기 위한 입력자료**다. 권장 수정은 다음 순서다.

1. `.kiro/steering/tech.md`의 '선택된 스택'과 'PoC만 하는 후보'를 분리한 `OSS_ADOPTION.md` 추가. 선택하지 않은 후보 라이브러리를 Kiro가 자동으로 설치하지 못하게 한다.
2. `docs/THIRD_PARTY_REGISTRY.md` 도입. 각 후보는 *사용버전·커밋·SPDX 라이선스·배포방식·수정여부·전이 의존성·LICENSE/NOTICE 위치·모델/코덱 조건·담당자*를 기록한 후 채택한다.
3. `docs/ADR/`에 대안 결정기록 작성: 업로더(S3 vs tus), 웹UI(Ant Design vs MUI), 작업큐(DB vs Celery), 에이전트(Go vs rclone), 권한(PolicyPort vs OPA), 카탈로그(Postgres vs OpenMetadata).
4. `05-asset-upload`에 Uppy S3 multipart 브라우저 어댑터 PoC, 중단/재개/버킷 직접 접근 차단 테스트 추가.
5. `06-sync-agent`에는 Go + SQLite를 유지하고 rclone을 선택적인 CLI/전환 어댑터로만 두기. `rclone sync` 삭제 전파 위험을 테스트하지 않고 자동 실행하지 않는다.
6. `07-ship-cctv-connectors`, `08-sensor-event`는 실제 앵커기업 장비·접근권한·네트워크가 확인되면 라이브러리 선택.
7. `09-processing`은 FFmpeg 실행파일 빌드 검증 및 DerivedAsset 계보/비식별 사람 검수 규칙을 유지.
8. `10-search-catalog`은 PostgreSQL 필터를 기준선으로 하고 인덱스·계약이 필요할 때 외부 카탈로그 도입을 심사.
9. `12-dataset-approval`은 플랫폼 도메인 승인 네 관문을 유지하고 외부 정책 엔진은 명시적 PoC 전까지 보류.
10. `17-quality-release`에 라이선스 스캔, SBOM, 재현 가능한 이미지 빌드와 배포 고지 파일 생성 작업 추가.

### Kiro에 전달할 실행 프롬프트

```text
먼저 AGENTS.md, .kiro/steering/tech.md, domain.md,
docs/CONTRACT_RULES.md와 이번 오픈소스 검토서를 읽어라.
기존 v0.06 아키텍처의 단일 계약과 organization_id·Blob/Asset 계보·PRIVATE 기본값을 유지한다.
오픈소스 59개를 자동으로 설치하지 말고 '채택'만 기본 구성으로,
'PoC'는 독립 실험 브랜치/테스트로, '조건부·후속·보류'는 설치하지 않는다.
지금은 소스코드 수정 전 기술선택 변경점과 ADR 초안, 영향을 받는 Spec과
라이선스 확인 체크리스트를 먼저 제시하라.
```

## 6. 도입 전 합격 기준 (예시)

| 검증 | 합격 기준 |
|---|---|
| 기업별 격리 | 기업 A 계정/토큰으로 B기업 Asset·Blob·메타데이터·서명 URL을 열람하거나 내려받을 수 없음 |
| 브라우저 업로드 | 10GB 파일을 40% 전송 후 중단·재개, 최종 해시 검증, 중복 Asset 생성 없음 |
| 에이전트 | 72시간 오프라인, 강제종료·재시작, 업로드 완료 재확인 및 네트워크 회복 후 안전 재개 |
| S3 교체 | 개발용 SeaweedFS와 다른 S3 호환 구현 간 계약 테스트 통과 |
| 비식별 | AI 처리 완료는 APPROVED가 아니며 작업 검수·적법성·제공기업 승인 전 다운로드 불가 |
| 보고서 | 실제 기관 양식, 근거 Asset 링크, 집계산식, 누락정보, 승인자 이력 유지 |
| 백업 | 중요 메타 DB·보고증빙·허용 데이터셋을 독립 위치에서 복원 검증 |
| 라이선스 | 정확한 배포 태그별 LICENSE/NOTICE, 빌드 구성, SBOM, 모델/코덱 목록 보존 |

## 7. 참고한 공개 공식 자료 (부분 검증)

- FFmpeg 라이선스·빌드 설정: https://github.com/FFmpeg/FFmpeg/blob/master/LICENSE.md
- FFmpeg 법적 고려사항: https://ffmpeg.org/legal.html
- Uppy S3 업로더: https://uppy.io/docs/aws-s3/
- Uppy Tus 대 S3 선택: https://uppy.io/docs/guides/choosing-uploader/
- SeaweedFS 공개코어: https://github.com/seaweedfs/seaweedfs
- SeaweedFS OSS/Enterprise 기능표: https://seaweedfs.com/docs/
- Meilisearch CE/EE: https://github.com/meilisearch/meilisearch
- authentik LICENSE: https://github.com/goauthentik/authentik/blob/main/LICENSE
- CVAT 구성 라이선스: https://github.com/cvat-ai/cvat/blob/develop/site/content/en/docs/getting_started/overview.md
- Grafana 기본 라이선스: https://github.com/grafana/grafana/blob/main/LICENSING.md
- CKAN 라이선스: https://github.com/ckan/ckan/blob/master/LICENSE.txt
- ERDDAP 공개 저장소: https://github.com/ERDDAP/erddap
- Schemathesis 라이선스: https://github.com/schemathesis/schemathesis/blob/master/LICENSE

**재확인 원칙:** 최신 main 브랜치는 검토 기준이지만 실제 도입 버전은 릴리스 태그/SHA로 고정하고 그 버전의 LICENSE, 의존성, 기능별 조건을 따로 확인한다. 이 문서는 법률의견서가 아니다.
