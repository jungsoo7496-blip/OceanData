# 설계: 06 동기화 에이전트

## 구조 (`agent/`)
| 패키지 | 역할 | 요구사항 |
|---|---|---|
| `cmd/marine-agent` | CLI: enroll·run·status·diag·install/uninstall/start/stop·update·rollback·version | 1, 11, 12 |
| `internal/api` | API 클라이언트. 시스템 CA + 선택 사설 CA, 검증 끄기 옵션 없음. 토큰 교환·401 재시도·ErrRevoked/ErrOffline 구분. 파트 PUT | 1.3, 5, 10 |
| `internal/config` | config.json, credentials.json(Windows CurrentUser DPAPI / Unix 0600), 서비스 계정 SID 확인 | 1.1 |
| `internal/contractapi` | oapi-codegen v2.8.2로 재생성하는 발신 계약 SDK. 필드 누락 검사는 api 어댑터에 유지 | 5, tech.md 2장 |
| `internal/rules` | `{session}`·글롭 경로 패턴, include/exclude, variant 허용표(domain.md 3장), 전송 순위, MIME | 2.1, 7.1 |
| `internal/state` | SQLite Local_State: 파일 상태(QUEUED/UPLOADING/DONE/EXCLUDED/FAILED), 재시도 시각, kv(정책·마지막 성공·재등록) | 5.2, 5.5 |
| `internal/core` | 스캔(쓰기 완료 판정)·fsnotify·해시·매니페스트·업로드·재개·하트비트·정책 갱신 루프 | 2~10 |
| `internal/sched` | 전송 허용 시간대(자정 넘김 포함)·토큰 버킷 대역폭·지수 백오프 | 6.1, 7.2, 7.3 |
| `internal/mediameta` | MP4/MOV mvhd creation_time (0·비현실 값은 근거 아님) | 4.2 |
| `internal/plugin` | Capture_Plugin·Sensor_Plugin 인터페이스 | 13 |
| `internal/update` | Ed25519·HTTPS·크기/시간 제한, 원자 교체, 워커 기동 감독·자동 복원·실행 잠금 | 12.1~12.3 |
| `internal/logx`, `internal/sysinfo` | 크기 순환 로그, 디스크 여유(Windows/Unix) | 11.4, 6.3 |

## 결정
- 멱등 키: `Idempotency-Key` = sha256(원천 장비|파일 해시|variant|회차 표시값|규칙)의 앞 32자 (`client.local_collection_id`와 같음).
- 서버 RULE_VIOLATION(422)은 "정책 제외"로 기록 — 서버 판단을 따른다 (2.4). QUOTA_EXCEEDED·RATE_LIMITED·STORAGE_UNAVAILABLE·CONFLICT는 백오프 재시도. 그 밖의 4xx는 FAILED.
- 세션 만료(410)·없음(404)이면 처음부터 다시 만든다. 대기 중 파일이 바뀌면 다시 판정한다.
- 계약 변경: `agent-heartbeat.schema.json`에 `policy_excluded`(정수) 추가 — 정책 제외 건수 보고 (2.4).
- 개발 편의 CLI (backend): `python -m app.cli set-policy <수집기> <정책.json>`(스키마·매핑 검증, 버전 증가, 감사), `python -m app.cli enrollment-code <수집기>`(운영자 대리 발급, 감사). 정식 화면은 09번.
- Go 툴체인 1.26.10: 1.24.7 표준 라이브러리 취약점(crypto/x509, net/http)을 govulncheck가 보고해 올렸다.

## 2026-11-05 로컬 보완 (ADR-0024·0025)

- complete 또는 ALREADY_REGISTERED만으로 바이너리를 DONE 처리하지 않는다. UPLOADING과 기존 kv에 영수증 대기 정보를 보존하고,
  등록 영수증의 자산 ID·해시·byte_size 필드 존재/값·등록시각·무결성 항목을 검증한 뒤 한 트랜잭션으로 영수증과 DONE을 저장한다.
  자산 격리·실패는 실패 처리한다. C0는 기존 메타등록 계약에 무결성 로그가 없으므로 별도 메타등록 완료 의미를 유지한다.
- 파일 감시와 전송의 상태 경합은 크기·mtime·SHA·자산/업로드·상태 스냅샷 조건으로 막는다. 오래된 영수증이 새 파일 버전을 완료로 만들지 않는다.
- DPAPI는 CurrentUser·UI 금지이며 평문/LocalMachine fallback이 없다. 구형 평문은 보호 저장 성공 후에만 사용한다.
  등록·설치 계정 SID가 다르면 설치를 거부한다. 서비스 암호·로그온 권한 설정은 OS 관리 절차로 남긴다.
- 자동 갱신 기본 꺼짐. 켠 경우 운영자가 정한 HTTPS 바이너리+base64 Ed25519 서명을 받는다.
  새 바이너리 자체의 SemVer로 이전 버전 재생을 거부하고, 검증 바이트만 교체한다.
  롤백도 검증한 바이트를 원자 설치해 백업 링크/경로가 나중에 바뀌는 영향을 받지 않는다.
  감독 프로세스는 준비 nonce·초기 생존을 확인하며 기동 실패 시 이전 파일과 워커를 복원한다. 인터넷 연결은 준비 조건이 아니다.
  Windows 워커·버전 확인 자식은 콘솔 없이 실행한다. OS 프로세스 생성 호출이 반환된 뒤 워커 준비 제한을 적용한다.
  실제 Windows 업데이트 14개와 제품 CLI 갱신·롤백을 재검증했다(ADR-0024).
- 진단 파일은 credentials·MQTT 계정/암호·설정 URL의 userinfo/query/fragment를 제외한다. 감시 원본을 묶지 않는다.
- T03-b는 실제 프로세스 강제 종료와 SQLite 재시작, 주입 시계 72시간·73회 거부 요청으로 시험한다.
  실제 72시간·PC 전원 차단·15번 엣지 검증은 별도다.
- 원본 API·서버 DB 구조 변경이 없어 신규 서버 마이그레이션/down은 해당 없음이다.
  SQLite도 기존 파일/kv 구조를 사용하므로 새 로컬 스키마 마이그레이션이 없다. 자격증명 보호 이행은 되돌릴 때 평문으로 내보내지 않고 같은 계정에서 재등록한다.

## 실제 스택 확인 (2026-11-04, `make up` 환경)
1. `set-policy`로 감시 규칙(`{session}/cam01/*` → CLIP, `{session}/sensor/*` → SENSOR ORIGINAL) 등록, `enrollment-code`로 코드 발급
2. `marine-agent enroll` → 같은 코드 재사용 시 서버 사유(GONE) 표시
3. 감시 폴더에 합성 영상(촬영시각 메타 있음)·CSV·.tmp → 사람 개입 없이 등록. 영상 capture_start_at = MP4 creation_time(ffprobe와 일치), CSV는 UNKNOWN·WARNING, .tmp 제외, 회차는 폴더 이름으로 결정 (T02)
4. 360MB 파일을 대역폭 제한으로 보내다 `kill -9` → 재시작 → "업로드 재개 completed_parts=2 pending=4" → 등록, sha256 일치, 영수증 (T03)
5. 자격증명 폐기 → 전송 중지, `status`에 "재등록 필요", 대기열 유지
6. `diag` zip에 credentials.json 없음, 원본 파일 그대로
