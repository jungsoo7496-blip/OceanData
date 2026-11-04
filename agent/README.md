# marine-agent — 동기화 에이전트 (06번 스펙)

현장 PC·엣지 박스에서 감시 폴더의 파일을 플랫폼으로 자동 전송한다. 단일 실행파일(Windows·Linux), 로컬 상태는 SQLite(순수 Go).

## 빌드
```bash
cd agent && go build -o bin/marine-agent ./cmd/marine-agent            # Linux
GOOS=windows go build -o bin/marine-agent.exe ./cmd/marine-agent       # Windows
go build -ldflags "-X main.version=0.1.0" -o bin/marine-agent ./cmd/marine-agent
```
Go 1.26.10 이상 (go.mod `toolchain`). 의존성: fsnotify(BSD-3), modernc.org/sqlite(BSD-3), kardianos/service(Zlib).

## 사용
```bash
marine-agent enroll --server https://api.example --code <등록코드> --watch-root /data/export [--ca 사설CA.pem]
marine-agent install && marine-agent start     # Windows 서비스 / systemd (관리자 권한)
marine-agent status                            # 연결 상태·대기 파일·마지막 성공·최근 오류
marine-agent diag                              # 진단 zip (자격증명 제외)
marine-agent run                               # 포그라운드 실행 (개발·문제 확인)
marine-agent update --file new --sig new.sig   # Ed25519 서명 검증 후 교체 (config.json의 update_public_key)
marine-agent rollback                          # 이전 버전으로
```
데이터 폴더: Linux `/var/lib/marine-agent`, Windows `%ProgramData%\MarineAgent` (`--dir` 또는 `MARINE_AGENT_DIR`).
`config.json`(설정), `credentials.json`(0600), `state.db`(0600), `logs/agent.log*`(크기 순환).

## 동작
1. 정책(`GET /v1/devices/{id}/policy`)의 감시 규칙으로 감시 루트 아래 파일을 분류한다. `{session}` 조각이 회차 표시값이 된다.
2. 크기·수정시각이 `size_stable_s` 동안 그대로이고 읽을 수 있으면 대기열에 넣는다 (첫 실행 때 기존 파일 포함).
3. 스트리밍 SHA-256 → 매니페스트(원래 파일명·상대 경로, 촬영시각은 MP4 내부 creation_time만) → `POST /v1/uploads`.
4. 파트를 병렬로 사전 서명 URL에 직접 보낸다. 끊기면 재시작 후 `GET .../parts`로 남은 파트만 보낸다.
5. 우선순위 C0 → C2(클립) → 그 외, 같은 순위는 오래된 것부터. 전송 허용 시간대·대역폭 한도를 지킨다.
6. 하트비트(대기량·원천 장비별·정책 제외 건수·디스크 여유·최근 오류). 오프라인이면 대기열을 유지하고 지수 백오프.
7. 401(자격증명 폐기)이면 전송을 멈추고 "재등록 필요"를 표시한다. 대기열은 유지한다.

**원본을 지우거나 옮기지 않는다.** 삭제 옵션도 없다 (domain.md 9장).

## 시험
```bash
go test -race ./...     # 가짜 API로 T02(자동 등록)·T03(끊긴 뒤 남은 파트만)·폐기·정책 제외·시간대 시험
```
실제 스택 확인 절차는 `.kiro/specs/06-sync-agent/design.md`의 "실제 스택 확인".
