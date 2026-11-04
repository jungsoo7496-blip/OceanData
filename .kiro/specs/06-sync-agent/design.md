# 설계: 06 동기화 에이전트

## 구조 (`agent/`)
| 패키지 | 역할 | 요구사항 |
|---|---|---|
| `cmd/marine-agent` | CLI: enroll·run·status·diag·install/uninstall/start/stop·update·rollback·version | 1, 11, 12 |
| `internal/api` | API 클라이언트. 시스템 CA + 선택 사설 CA, 검증 끄기 옵션 없음. 토큰 교환·401 재시도·ErrRevoked/ErrOffline 구분. 파트 PUT | 1.3, 5, 10 |
| `internal/config` | config.json, credentials.json(0600, 권한 넓으면 거부) | 1.1 |
| `internal/rules` | `{session}`·글롭 경로 패턴, include/exclude, variant 허용표(domain.md 3장), 전송 순위, MIME | 2.1, 7.1 |
| `internal/state` | SQLite Local_State: 파일 상태(QUEUED/UPLOADING/DONE/EXCLUDED/FAILED), 재시도 시각, kv(정책·마지막 성공·재등록) | 5.2, 5.5 |
| `internal/core` | 스캔(쓰기 완료 판정)·fsnotify·해시·매니페스트·업로드·재개·하트비트·정책 갱신 루프 | 2~10 |
| `internal/sched` | 전송 허용 시간대(자정 넘김 포함)·토큰 버킷 대역폭·지수 백오프 | 6.1, 7.2, 7.3 |
| `internal/mediameta` | MP4/MOV mvhd creation_time (0·비현실 값은 근거 아님) | 4.2 |
| `internal/plugin` | Capture_Plugin·Sensor_Plugin 인터페이스 | 13 |
| `internal/update` | Ed25519 서명 검증, .bak 백업·되돌리기 | 12.1~12.3 |
| `internal/logx`, `internal/sysinfo` | 크기 순환 로그, 디스크 여유(Windows/Unix) | 11.4, 6.3 |

## 결정
- 멱등 키: `Idempotency-Key` = sha256(원천 장비|파일 해시|variant|회차 표시값|규칙)의 앞 32자 (`client.local_collection_id`와 같음).
- 서버 RULE_VIOLATION(422)은 "정책 제외"로 기록 — 서버 판단을 따른다 (2.4). QUOTA_EXCEEDED·RATE_LIMITED·STORAGE_UNAVAILABLE·CONFLICT는 백오프 재시도. 그 밖의 4xx는 FAILED.
- 세션 만료(410)·없음(404)이면 처음부터 다시 만든다. 대기 중 파일이 바뀌면 다시 판정한다.
- 계약 변경: `agent-heartbeat.schema.json`에 `policy_excluded`(정수) 추가 — 정책 제외 건수 보고 (2.4).
- 개발 편의 CLI (backend): `python -m app.cli set-policy <수집기> <정책.json>`(스키마·매핑 검증, 버전 증가, 감사), `python -m app.cli enrollment-code <수집기>`(운영자 대리 발급, 감사). 정식 화면은 09번.
- Go 툴체인 1.26.10: 1.24.7 표준 라이브러리 취약점(crypto/x509, net/http)을 govulncheck가 보고해 올렸다.

## 실제 스택 확인 (2026-11-04, `make up` 환경)
1. `set-policy`로 감시 규칙(`{session}/cam01/*` → CLIP, `{session}/sensor/*` → SENSOR ORIGINAL) 등록, `enrollment-code`로 코드 발급
2. `marine-agent enroll` → 같은 코드 재사용 시 서버 사유(GONE) 표시
3. 감시 폴더에 합성 영상(촬영시각 메타 있음)·CSV·.tmp → 사람 개입 없이 등록. 영상 capture_start_at = MP4 creation_time(ffprobe와 일치), CSV는 UNKNOWN·WARNING, .tmp 제외, 회차는 폴더 이름으로 결정 (T02)
4. 360MB 파일을 대역폭 제한으로 보내다 `kill -9` → 재시작 → "업로드 재개 completed_parts=2 pending=4" → 등록, sha256 일치, 영수증 (T03)
5. 자격증명 폐기 → 전송 중지, `status`에 "재등록 필요", 대기열 유지
6. `diag` zip에 credentials.json 없음, 원본 파일 그대로
