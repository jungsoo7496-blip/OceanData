# marine-agent — 동기화 에이전트 (06번 스펙)

현장 PC·엣지 박스에서 감시 폴더의 파일을 플랫폼으로 자동 전송한다. 단일 실행파일(Windows·Linux), 로컬 상태는 SQLite(순수 Go).

## 빌드
```bash
cd agent && go build -o bin/marine-agent ./cmd/marine-agent            # Linux
GOOS=windows go build -o bin/marine-agent.exe ./cmd/marine-agent       # Windows
go build -ldflags "-X main.version=0.1.0" -o bin/marine-agent ./cmd/marine-agent
```
Go 1.26.10 이상 (go.mod `toolchain`). 주요 의존성: fsnotify(BSD-3), modernc.org/sqlite(BSD-3), kardianos/service(Zlib), go-selfupdate/update(MIT), oapi-codegen/runtime(Apache-2.0).

## 사용
```bash
marine-agent enroll --server https://api.example --code <등록코드> --watch-root /data/export [--ca 사설CA.pem]
marine-agent install --service-user 'DOMAIN\collector'  # 등록한 Windows 계정과 같은 계정
marine-agent start                             # Windows 서비스 / systemd (관리자 권한)
marine-agent status                            # 연결 상태·대기 파일·마지막 성공·최근 오류
marine-agent diag                              # 진단 zip (자격증명 제외)
marine-agent run                               # 포그라운드 실행 (개발·문제 확인)
marine-agent update --file new --sig new.sig   # Ed25519 서명 검증 후 교체 (config.json의 update_public_key)
marine-agent rollback                          # 이전 버전으로
```
데이터 폴더: Linux `/var/lib/marine-agent`, Windows `%ProgramData%\MarineAgent` (`--dir` 또는 `MARINE_AGENT_DIR`).
`config.json`(설정), `credentials.json`(Windows CurrentUser DPAPI / Unix 0600), `state.db`(0600), `logs/agent.log*`·`logs/supervisor.log*`(크기 순환).

Windows에서는 등록·서비스 실행 계정이 같아야 한다. `install --service-user`는 현재 계정과 서비스 계정의 SID를 비교한다.
계정 옵션을 생략하면 기본 LocalSystem과 비교하므로 일반 사용자 등록 후 생략하면 설치를 거부한다.
서비스 로그온 권한과 암호는 Windows 관리 도구에서 별도로 설정한다. CLI·설정 파일에는 서비스 암호를 받지 않는다.
구형 평문 자격증명은 같은 계정에서 읽을 때 DPAPI로 원자 교체하며, 보호 실패 시 평문 사용을 거부한다.
다른 계정으로 이전하거나 복호화할 수 없으면 같은 서비스 계정으로 재등록한다. LocalMachine·평문 대체 옵션은 없다.

## 동작
1. 정책(`GET /v1/devices/{id}/policy`)의 감시 규칙으로 감시 루트 아래 파일을 분류한다. `{session}` 조각이 회차 표시값이 된다.
2. 크기·수정시각이 `size_stable_s` 동안 그대로이고 읽을 수 있으면 대기열에 넣는다 (첫 실행 때 기존 파일 포함).
3. 스트리밍 SHA-256 → 매니페스트(원래 파일명·상대 경로, 촬영시각은 MP4 내부 creation_time만) → `POST /v1/uploads`.
4. 파트를 병렬로 사전 서명 URL에 직접 보낸다. 끊기면 재시작 후 `GET .../parts`로 남은 파트만 보낸다.
   complete의 `RECEIVED`는 완료가 아니다. 실제 영수증의 자산 ID·해시·크기·등록시각·무결성 항목을 확인하고
   SQLite에 영수증과 DONE을 함께 저장한다. 영수증이 404이면 다음 재시도까지 대기한다.
   C0는 바이너리·무결성 항목을 만들지 않는 기존 계약이므로 메타데이터 등록 응답으로 완료 처리한다.
5. 우선순위 C0 → C2(클립) → 그 외, 같은 순위는 오래된 것부터. 전송 허용 시간대·대역폭 한도를 지킨다.
6. 하트비트(대기량·원천 장비별·정책 제외 건수·디스크 여유·최근 오류). 오프라인이면 대기열을 유지하고 지수 백오프.
7. 401(자격증명 폐기)이면 전송을 멈추고 "재등록 필요"를 표시한다. 대기열은 유지한다.

**원본을 지우거나 옮기지 않는다.** 삭제 옵션도 없다 (domain.md 9장).

## 서명 업데이트

자동 갱신은 기본 꺼짐이며 꺼진 상태에서는 업데이트 요청을 보내지 않는다. 운영 배포 주소·키는 **확인 필요**다.
운영자가 `config.json`에 다음 필드를 설정하고 서비스를 재시작한다. 실제 공개키는 신뢰할 수 있는 경로로 배포한다.

```json
{
  "auto_update": false,
  "update_public_key": "<base64 Ed25519 공개키>",
  "update_url": "https://updates.example.invalid/marine-agent",
  "update_signature_url": "https://updates.example.invalid/marine-agent.sig",
  "update_interval_s": 3600,
  "update_timeout_s": 120,
  "update_max_bytes": 67108864,
  "update_startup_timeout_s": 30,
  "update_stable_s": 5
}
```

서명 파일은 바이너리 전체의 Ed25519 서명을 base64로 담는다(기존 수동 포맷).
HTTPS·서버 인증서 검증·다운로드 크기/시간 제한을 적용하고 HTTP로의 리디렉션을 거부한다.
검증된 바이너리의 `version` 출력(SemVer)이 현재 버전보다 클 때만 교체한다.
수동 업데이트는 서비스를 멈춘 뒤 실행한다. 실행 잠금이 있으면 중복 교체를 거부한다.

감독 프로세스는 워커의 로컬 준비와 초기 생존을 확인한다. 새 버전이 시작하지 못하거나 준비 중 종료하면 이전 파일을 복원하고,
자동 갱신에서는 이전 워커도 다시 시작한다. 오프라인 자체는 기동 실패 조건이 아니다.
`rollback`은 기록된 이전 파일의 해시를 확인해 복원한다. 실제 Windows 서비스 설치·부팅·권한 검증은 현장 확인 항목이다.
Windows 업데이트 자식은 콘솔 없이 실행한다. 준비 제한 시간은 OS가 자식 생성 호출을 반환한 뒤 적용한다.

## 계약 SDK

저장소 루트에서 `make agent-generate`로 `internal/contractapi/client.gen.go`를 생성하고
`make agent-generate-check`로 미재생성 여부를 검사한다(Go·`make venv` 필요).
고정 버전 oapi-codegen v2.8.2를 사용하며 생성 파일은 손으로 편집하지 않는다.
원본 JSON Schema의 로컬 참조를 생성용으로만 투영하고, API·도메인 코드값은 유지한다.
인증·재시도와 필드 누락 검사는 `internal/api` 어댑터가 맡는다. 생성 타입이 전체 JSON Schema 검증을 대신하지 않는다(ADR-0025).

## 시험
```bash
go test -race ./...     # 가짜 API로 T02(자동 등록)·T03(끊긴 뒤 남은 파트만)·폐기·정책 제외·시간대 시험
```
`TestT03ForcedKillAnd72HourOfflineResume`는 실제 자식 프로세스를 강제 종료하고 SQLite를 다시 연 뒤,
주입한 시계로 72시간·73회 오프라인 요청을 재현한다. 원본 불변·저장 파트 재전송 없음·복구 후 자동 재개·영수증 저장을 확인한다.
이는 실제 72시간 대기·PC 전원 차단·15번 엣지 장비 시험을 대신하지 않는다.
실제 스택 확인 절차는 `.kiro/specs/06-sync-agent/design.md`의 "실제 스택 확인".

## MQTT 브로커 암호 (07번)

`config.json`의 `sensor_plugins[].password`에 암호를 넣고 에이전트를 시작하면, 시작할 때 보안 저장소
(`<데이터 폴더>/secrets/mqtt-<장비 ID>.json`, Windows는 실행 계정의 DPAPI, Unix는 0600)로 옮기고 설정에는
`password_secret`(이름)만 남긴다. 암호를 바꾸려면 다시 `password`에 새 값을 넣고 재시작한다. 비밀 파일이 없으면
빈 암호로 붙지 않고 시작을 멈춘다. 진단 묶음에는 사용자 이름·암호를 넣지 않는다.
