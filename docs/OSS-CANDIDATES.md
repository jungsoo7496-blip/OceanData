# 오픈소스 재사용 판정 (v0.2 기준)

입력: 사용자 정리표 "GitHub 재사용 후보" (gptv0.05의 17개 스펙 기준, 후보 59건, 2026-11-04).
이 문서는 그 59건을 **v0.2 스펙 번호·설계 결정·tech.md 라이선스 허용 목록**에 맞춰 다시 판정하고, 표에 없던 후보를 더한 것이다.
법률 의견이 아니라 개발팀 1차 분류다. 도입 전 실제 버전의 LICENSE·NOTICE·전이 의존성을 다시 확인한다.

## 판정 기준
| 판정 | 뜻 | Kiro 사용 규칙 |
|---|---|---|
| 채택 | 지금 쓴다 (이미 스택에 있거나 설계와 바로 맞음) | 바로 사용 |
| PoC | 작은 시험으로 결정한다 | PoC 결과 ADR이 있어야 사용 |
| 조건부 | 특정 요구가 확인될 때만 | 조건 확인 + ADR 후 사용 |
| 안 씀 | 설계 결정과 충돌하거나 부담이 큼 | 사용 금지 (바꾸려면 ADR) |

라이선스 허용 목록(tech.md 3장): MIT, BSD, Apache-2.0, ISC, MPL-2.0, PostgreSQL, Zlib (+ 폰트는 OFL-1.1).
별도 프로세스로만 실행하는 인프라(PostgreSQL, Keycloak, SeaweedFS, FFmpeg, Mosquitto 등)는 목록 적용 대상이 아니지만 빌드·고지는 기록한다.

## 1. 사용자 표 59건 판정

### 기반·인증 (01·02)
| 후보 | 라이선스 | 판정 | 이유·쓰는 방식 |
|---|---|---|---|
| FastAPI | MIT | 채택 | 이미 스택 |
| SQLAlchemy | MIT | 채택 | 이미 스택 |
| Celery | BSD-3 | 안 씀 | 브로커(Redis·RabbitMQ)가 필요해 구성요소가 늘고 ADR-0002(PostgreSQL 큐)와 충돌. Redis는 7.4부터 RSALv2/SSPL, 8.0부터 AGPL 선택지라 공공 납품 검토 부담도 생김. 대신 procrastinate(아래 2장) |
| Keycloak | Apache-2.0 | 채택 | 이미 스택 |
| Casdoor | Apache-2.0 | 안 씀 | Keycloak으로 충분. 비교 PoC 비용을 아낀다 |
| authentik | MIT(Core)+유료 기능 | 안 씀 | 같은 이유. 유료 기능 경계 관리 부담 |

### 데이터 표준·센서 (03·07·12)
| 후보 | 라이선스 | 판정 | 이유·쓰는 방식 |
|---|---|---|---|
| python-jsonschema | MIT | 채택 | `tools/validate_contracts.py`가 이미 사용 |
| Frictionless (frictionless-py) | MIT | PoC | 07번 CSV 매핑 프로필을 Frictionless Table Schema 형식으로 쓰고 파싱·결측 표기 처리에 사용 |
| Pandera | MIT | 안 씀 | Frictionless·jsonschema와 겹침 |
| Eclipse Mosquitto | EPL-2.0 / EDL-1.0 | 채택 (개발·시험용만) | 07번 MQTT 플러그인 통합 시험용 브로커. 센터에는 MQTT를 열지 않는다 |
| Telegraf | MIT | 조건부 | 센서 기업이 Modbus·시리얼 등 여러 프로토콜을 쓸 때 현장 수집기로. HTTP 출력 → `POST /v1/observations` 형식 변환과 토큰 발급 연동을 PoC. MQTT만이면 에이전트 플러그인으로 충분 |
| Node-RED | Apache-2.0 | 안 씀 | 운영 흐름의 변경 감사·버전 관리가 어렵다 (기업 시연용 PoC는 가능) |
| FROST Server | LGPL-3.0 | 조건부 | OGC SensorThings 연계 의무가 확인될 때만, 별도 서비스로 (16번) |

### 업로드·저장·동기화 (04·06·08)
| 후보 | 라이선스 | 판정 | 이유·쓰는 방식 |
|---|---|---|---|
| tusd | MIT | 안 씀 | tus 서버가 바이너리를 직접 받는다. "스토리지 직송" 원칙·S3 멀티파트 계약(contracts)과 다름 |
| Uppy | MIT | 채택 | 09번 웹 업로드. `@uppy/aws-s3` 멀티파트 모드의 서명 함수를 우리 API(`POST /v1/uploads`, `GET …/parts`, `POST …/complete`)에 연결. 탭을 닫았다 열 때의 재개 방식(대용량은 파일 재선택 후 남은 파트만)은 PoC로 확인 |
| SeaweedFS | Apache-2.0 | 채택 (개발·통합 시험) | 이미 개발 환경 S3. 운영 저장소 후보 중 하나 (G2에서 결정) |
| rclone | MIT | PoC (용도 변경) | 에이전트로는 안 씀 — 현장에 장기 S3 키가 필요해 기준선 8.4장 위반. 대신 08번 백업(존 사이 객체 복사·검증)과 오프라인 디스크 이관(ING-11) 도구로 |
| Syncthing | MPL-2.0 | 안 씀 | 양방향 동기화. 단방향 수집·회차 연결 규칙이 없음 |
| watchdog | Apache-2.0 | 해당 없음 | 에이전트가 Go다. Go에서는 fsnotify (2장) |

### 엣지·CCTV (15)
| 후보 | 라이선스 | 판정 | 이유·쓰는 방식 |
|---|---|---|---|
| MediaMTX | MIT | PoC | 엣지 RTSP 녹화(세그먼트 파일)와 개발용 모의 카메라(ffmpeg 테스트 영상 → RTSP). 에이전트는 녹화 폴더만 감시 |
| go2rtc | MIT | 안 씀 | MediaMTX와 겹침 (필요하면 대안) |
| python-onvif-zeep | MIT | 조건부 (조사 도구) | 연계 카드 단계에서 NVR의 녹화 검색·반출 지원 여부 확인용. 운영 코드는 Go. 저장소 유지보수 상태 확인 |

### 처리·비식별 (05·14)
| 후보 | 라이선스 | 판정 | 이유·쓰는 방식 |
|---|---|---|---|
| FFmpeg | LGPL 또는 GPL (빌드별) | 채택 | 이미 스택 (별도 프로세스). libx264 같은 GPL 구성을 넣으면 납품 이미지에 GPL 고지·소스 제공 조건이 붙으므로 빌드 구성을 기록하고 고지에 포함 |
| OpenCV | Apache-2.0 (4.5+) | 채택 | 프레임 처리·가림 렌더링. 탐지 모델은 별도 라이선스 판단 (2장) |
| CVAT Community | MIT | 조건부 | 라벨링 작업(P1~P2)이 실제로 생길 때 별도 도구로 연계. 비식별 검수 화면은 가볍게 직접 만든다 (구성 서비스가 많아 운영 부담) |
| Presidio | MIT | 보류 | 문서·메타데이터의 개인정보 탐지가 필요할 때. 한국어는 사용자 정의 인식기 필요. Microsoft에서 커뮤니티 프로젝트(data-privacy-stack)로 이관됨 |

### 검색·카탈로그·공유 (09·13)
| 후보 | 라이선스 | 판정 | 이유·쓰는 방식 |
|---|---|---|---|
| OpenMetadata | Apache-2.0 | 안 씀 (현 단계) | 별도 검색엔진·DB 운영 부담. 카탈로그는 PostgreSQL로 충분 |
| DataHub | Apache-2.0 | 안 씀 (현 단계) | 같은 이유 (메시지 브로커 등) |
| Meilisearch CE | MIT (EE는 BUSL) | 안 씀 | PostgreSQL 검색으로 시작. 별도 색인은 테넌트 필터를 두 번 구현하게 됨 |
| CKAN | AGPL-3.0 | 안 씀 | AGPL, 공개 포털 구조. DCAT 내보내기(13번)로 대신 |
| Open Policy Agent (×2) | Apache-2.0 | 안 씀 | 공유 조건은 domain/sharing_rules 한 곳에 코드로 두고 속성 기반 테스트로 검증한다. 정책 엔진을 두면 규칙이 두 곳이 된다 |
| OpenFGA | Apache-2.0 | 안 씀 | 같은 이유 |
| Casbin | Apache-2.0 | 안 씀 | Permission_Table이 단순 선언표라 불필요 |
| boto3 | Apache-2.0 | 채택 | 이미 스택 |
| Apache APISIX | Apache-2.0 | 조건부 | 통합 단계 게이트웨이 후보 (tech.md와 같음) |

### 보고서 (10)
| 후보 | 라이선스 | 판정 | 이유·쓰는 방식 |
|---|---|---|---|
| WeasyPrint | BSD-3 | 채택 | 이미 스택. 한글 폰트는 OFL 폰트 (2장) |
| docxtpl | LGPL-2.1 | 안 씀 | Word 산출물 요구가 없다 (필요한 것은 HWPX). LGPL은 허용 목록 밖 |
| Apache Superset | Apache-2.0 | 안 씀 | 현 단계 BI 불필요. 지표 화면은 앱 안에서 |

### 운영·웹·표준·품질 (09·11·16·17)
| 후보 | 라이선스 | 판정 | 이유·쓰는 방식 |
|---|---|---|---|
| Prometheus | Apache-2.0 | 채택 | 이미 스택 |
| Grafana | AGPL-3.0 | 조건부 | 수정하지 않고 운영자 내부용 별도 서비스로만 (compose 선택). 외부 사용자 노출·수정·임베드 금지. P0 운영 화면은 11번에서 앱 안에 |
| OpenTelemetry Collector | Apache-2.0 | 조건부 | 분산 추적이 필요해질 때 |
| restic | BSD-2 | PoC | 08번 설정·내보내기 백업(암호화 스냅샷). DB는 pgBackRest와 비교 |
| MUI | MIT (MUI X 고급 표는 유료) | 안 씀 | tech.md가 Ant Design(ko_KR)으로 정함. 관리 화면의 고급 표 기능이 유료 등급에 있어 막힘 |
| TanStack Query | MIT | 채택 | 이미 스택 |
| React Hook Form | MIT | PoC | Ant Design Form과 둘 중 하나로 통일 (Refine PoC에서 결정) |
| Refine | MIT (Core) | PoC | 09번 운영 콘솔·장비·회차 같은 CRUD 화면. Ant Design 연동(@refinedev/antd)이 있어 스택과 맞음 |
| React-admin | MIT (Enterprise 별도) | 안 씀 | MUI 기반이라 Refine와 겹침 |
| JSON Forms | MIT | 조건부 | 연계 카드·장비 정책 폼을 contracts 스키마로 만들 때. Ant Design 쪽은 react-jsonschema-form(@rjsf/antd)과 비교 |
| pygeoapi | MIT | 조건부 | OGC API 연계 의무가 확인될 때 (16번) |
| stac-fastapi | MIT | 조건부 | STAC 수요가 생길 때 (기준선 7.5장 P1) |
| ERDDAP | 확인 필요 | 안 씀 | 라이선스 불명확, 목적이 다름 |
| RDFLib | BSD-3 | 조건부 | DCAT을 RDF로 다뤄야 할 때. 지금은 JSON-LD를 직접 만든다 |
| pytest | MIT | 채택 | |
| Schemathesis | MIT | 채택 | `contracts/openapi.yaml` 기준 계약 시험 (04번 요구사항 10) |
| Playwright | Apache-2.0 | 채택 | 화면 E2E. 접근성은 @axe-core/playwright 함께 |
| Trivy | Apache-2.0 | 채택 | 17번 취약점 검사 |
| Syft | Apache-2.0 | 채택 | 17번 SBOM → 라이선스 허용 목록 검사 |
| OWASP ZAP | Apache-2.0 | 채택 | 17번 동적 보안 검사. 교차 기관 접근은 T01로 따로 |

## 2. 표에 없던 추가 후보
| 후보 | 라이선스 | 판정 | 스펙 | 줄어드는 개발 |
|---|---|---|---|---|
| procrastinate | MIT | PoC | 05 | PostgreSQL 기반 작업 큐. 문서상 우선순위·재시도 전략·주기 작업·멈춘 작업 재시도를 지원. 유지보수 인력을 모집 중이라는 공지가 있어 장기 유지보수 위험을 ADR에 적는다. 요구사항(05번 1장)을 못 맞추면 자체 구현 |
| fsnotify | BSD-3 | 채택 | 06 | 폴더 감시 |
| modernc.org/sqlite | BSD-3 | 채택 | 06 | 순수 Go SQLite (Local_State) |
| kardianos/service | Zlib | 채택 | 06 | Windows 서비스·systemd 등록 |
| creativeprojects/go-selfupdate | MIT | 교체 primitive 채택 | 06 | v1.7.0 Linux·Windows 실제 프로세스 PoC 후 update 하위 패키지만 채택. 서명·기동 감독은 자체 계층 (ADR-0024) |
| eclipse/paho.mqtt.golang | EPL-2.0 / EDL-1.0 (EDL 선택) | 채택 | 07 | Sensor_Plugin MQTT 구독 |
| pgBackRest | MIT | PoC | 08 | DB 백업·시점 복구 |
| datamodel-code-generator | MIT | 채택 | 03 | contracts JSON Schema → Pydantic 모델 |
| openapi-typescript | MIT | 채택 | 09 | contracts OpenAPI → TypeScript 타입 |
| oapi-codegen | Apache-2.0 | 채택 | 06 | contracts OpenAPI → Go 클라이언트 |
| Locust | MIT | 채택 | 17 | 부하 시험 (T12) |
| @axe-core/playwright | MPL-2.0 | 채택 | 09·17 | KWCAG 자동 검사 보조 |
| 한글 폰트 (Noto Sans KR 등) | OFL-1.1 | 채택 | 10 | PDF 한글 출력 |
| ONNX Runtime | MIT | 채택 | 05·14 | 이미 스택 |

**HWPX:** 공개 라이브러리 중 pyhwpxlib는 일부 파일만 Apache-2.0이고 나머지는 BSL 1.1(상업 이용·6인 이상 사용 시 유료 라이선스)이라 쓰지 않는다. tech.md대로 HWPX 템플릿 XML 치환을 스파이크로 확정한다.

**탐지 모델 가중치:** 코드 라이선스와 별개로 판단한다. Ultralytics YOLO 계열(AGPL-3.0)과 InsightFace 사전학습 모델(비상업 조건)은 승인 없이 쓰지 않는다. 허용 라이선스 모델(예: Apache-2.0으로 배포된 탐지 모델)도 학습 데이터 이용 조건을 Model_Registry에 함께 기록한다.

## 3. 직접 만들어야 남는 것
- 도메인 규칙: 공유 가능 조건, 상태 전이, 멱등성, 촬영시각 규칙 (domain.md)
- API·서비스 결합: 업로드 세션·쿼터 예약·무결성 검증·격리, 관측값·이벤트 수신, 공유 심사·이용 신청·배포
- 에이전트 본체: 감시 규칙 → 매니페스트 → 파트 업로드 → 재개 → 하트비트 (부품은 위 라이브러리)
- 실증 업무 화면 흐름: 워크스페이스, 회차 타임라인, 증빙·보고서, 관리기관 보드
- 보고서 템플릿(HWPX 포함), 비식별 검수 화면, 수집 정상성 규칙

## 4. PoC 순서 (스펙 진행 순서와 같다)
| 순서 | PoC | 결정할 것 | 관련 수용시험 |
|---|---|---|---|
| 1 | procrastinate | 05번 큐를 자체 구현할지 | T04-f, T12-c |
| 2 | Frictionless | 07번 CSV 매핑 프로필 형식 | T05-d |
| 3 | pgBackRest + rclone (+restic) | 08번 백업 도구 조합 | T10 |
| 4 | Uppy + 계약 API | 09번 웹 업로드 재개 방식 | T03-a, T02-g |
| 5 | Refine + Ant Design | 09번 운영 콘솔 CRUD를 프레임워크로 갈지, RHF와 antd Form 중 무엇으로 통일할지 | T01-a(화면) |
| 6 | MediaMTX | 15번 엣지 RTSP 녹화·모의 카메라 | T02-e |
| 7 | Telegraf | 07번 — 앵커 센서 기업의 통신 방식 확인 뒤에만 | T05-d, T07-c |

각 PoC는 해당 스펙을 시작하기 전에 끝낸다.

PoC마다 `docs/adr/`에 결과(버전·커밋, 라이선스 파일 확인, 합격 여부, 되돌리는 방법)를 남긴다.

## 5. 라이선스·납품 점검
사용자 표의 "라이선스·납품" 12단계(버전·커밋 고정, LICENSE/NOTICE 수집, 사용 방식 구분, 모델·코덱 별도 점검, SBOM, 취약점 검사, 과제 협약 확인, 개발 실적 분리, 데이터 권리 분리, THIRD_PARTY_NOTICES, 운영 적합성, 근거 보존)를 그대로 채택한다.
그중 SBOM·라이선스 검사·취약점 검사는 17번 릴리스 게이트에서 자동으로 확인한다.
특히 "오픈소스 라이선스 허용 ≠ 실증기업 데이터 이용권"은 domain.md의 약정 규칙(1단·2단)으로 따로 관리한다.
