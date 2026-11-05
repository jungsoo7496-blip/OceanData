# ADR-0032: golangci-lint는 go.mod 밖의 개발 도구로만 실행한다 (GPL-3.0)

- 상태: accepted
- 날짜: 2026-11-05
- 관련: 01번 작업 17·요구사항 9.1, tech.md 3장(라이선스 허용 목록), AGENTS.md 6번, `agent/.golangci.yml`

## 맥락
01번 작업 17은 에이전트(Go) 코드에 golangci-lint를 거는 일이다. golangci-lint는 GPL-3.0이다. tech.md 3장의 허용 목록은
"코드에 포함·링크되는 라이브러리"를 대상으로 하므로, 납품 바이너리·이미지에 섞이지 않는 방식으로만 써야 한다.
OSS-CANDIDATES 목록에도 없었다.

## 결정
1. **별도 프로세스 개발 도구로만 실행한다**: `go run github.com/golangci/golangci-lint/v2/cmd/golangci-lint@v2.15.0 run ./...`
   (CI agent 작업, `make agent-lint`). `agent/go.mod`·`go.sum`에 넣지 않는다 → 제품 모듈 그래프·SBOM·라이선스 검사에 나타나지 않고
   `marine-agent` 바이너리에도 링크되지 않는다. 버전은 v2.15.0으로 고정한다(Go 1.26 이상 필요, CI는 1.26.10).
2. **설정**: v2 형식, `standard`(errcheck·govet·ineffassign·staticcheck·unused) + gofmt 포매터. 제외 규칙·`//nolint`를 두지 않는다.
   버리는 오류는 코드에서 `_ =`로 드러내고, 쓰기 파일의 닫기 오류는 확인한다.
3. **Windows 전용 파일도 검사**: 두 번째 실행은 `go run -exec "env GOOS=windows" …`로 린터는 실행 중인 컴퓨터용으로 빌드하고
   분석만 Windows 대상으로 한다(`GOOS=windows go run`은 Windows용 린터 바이너리를 만들어 Linux에서 실행하지 못한다).

버린 대안:
- `go.mod`의 `tool` 지시어로 고정: 버전 관리는 편하지만 GPL 모듈이 제품 모듈 그래프·`go.sum`에 들어가 라이선스 검사·SBOM과 섞인다.
- golangci-lint-action: CI만 따로 바이너리를 받아 로컬 `make agent-lint`와 버전을 맞추기 어렵다.
- staticcheck(MIT) 단독: 작업 17이 golangci-lint를 지정했고, errcheck·ineffassign을 따로 엮어야 한다.

## 결과
- 좋아지는 점: 닫기 오류·채널 오류 누락 같은 결함을 CI에서 막는다(처음 지적 43건을 모두 코드에서 처리, 제외 0건).
  쓰기 대상(감독 로그·워커 로그·상태 DB·진단 zip)의 닫기 실패는 이제 오류로 돌려준다 — 감독 로그 닫기 실패 시 종료 코드 1.
- 감수할 점: `go run …@버전`은 처음 실행 때 모듈을 내려받는다(사내망 CI에서는 Go 모듈 프록시 미러 필요 — 확인 필요).
- 되돌리는 방법: CI 두 단계와 `make agent-lint`, `agent/.golangci.yml`을 지운다. 제품 코드·의존성에는 남는 것이 없다.
