# ADR-0024: 에이전트 자격증명 보호와 서명 업데이트 감독

- 상태: accepted (플랫폼별 검증 결과는 06번 작업 목록에 별도 기록)
- 날짜: 2026-11-05
- 관련: 06번 요구사항 1.1·11·12, security.md, OSS-CANDIDATES.md go-selfupdate PoC

## 맥락

Windows의 기존 credentials.json은 평문이었고, 수동 업데이트는 서명 검사 후 파일을 다시 열어 교체했다.
새 버전의 실제 기동 실패를 자동으로 되돌리는 감독도 없었다. 기존 서버/API·도메인 규칙은 바꾸지 않는다.

## PoC 근거

- creativeprojects/go-selfupdate v1.7.0, 커밋 `caf24d238b2068427363fdbd7da928f73a2e6cb2`, MIT 원문 확인.
- 합성 Linux·Windows 바이너리와 실제 자식 프로세스로 실행 중 파일의 원자 교체, Ed25519 변조 거부,
  파일 되돌리기를 확인했다. 새 바이너리가 exit 7로 종료돼도 라이브러리는 교체 파일을 유지해 기동 실패 롤백은 제공하지 않았다.
- PoC 소스·실행 결과는 `work/selfupdate-poc/` 중간 산출물에 보관한다.
  근거 소스: [v1.7.0](https://github.com/creativeprojects/go-selfupdate/tree/v1.7.0).

## 결정

1. Windows는 CurrentUser DPAPI와 UI 금지 옵션으로 보호한다. LocalMachine·평문 대체 경로는 두지 않는다.
   Unix는 기존 0600 파일 보호를 유지한다. 평문 구형 파일은 엄격히 읽고 보호·원자 저장 성공 후에만 이행 성공으로 본다.
   복호화 실패는 고정된 한국어 오류로 처리하고 비밀값·암호문을 로그나 진단 파일에 넣지 않는다.
2. 등록과 서비스 실행 계정이 같아야 한다. CurrentUser 암호문을 다른 계정인 기본 LocalSystem이 읽는 문제를 숨기지 않는다.
   서비스 설치 계정의 명시·검증과 같은 계정 등록 절차를 제공한다. 계정 비밀번호를 CLI 인자나 config.json에 저장하지 않는다.
3. go-selfupdate 전체 업데이터 대신 `github.com/creativeprojects/go-selfupdate/update`의 파일 교체 primitive를 채택한다.
   네트워크·서명 검증·크기 제한·기동 감독·자동 롤백은 자체 얇은 계층에서 수행한다.
   버전 비교는 같은 라이브러리의 기존 의존성 Masterminds/semver/v3(MIT)를 사용한다.
4. 자동 갱신은 기본 꺼짐이다. 운영자가 켠 경우에만 설정한 HTTPS 바이너리·별도 서명 URL을 조회한다.
   기존 수동 명령과 같은 바이너리 전체에 대한 Ed25519 서명 포맷을 사용한다. 외부 배포 API나 미확인 JSON 명세를 만들지 않는다.
   새 버전 확인은 검증된 바이너리에서 얻고 자동 다운그레이드를 거부한다.
5. 크기·시간 제한과 TLS 검증을 강제한다. HTTPS에서 HTTP로의 리다이렉트를 허용하지 않는다.
   서명 검증한 파일 자체를 교체해 검증 후 재열기 간 파일 변경 위험을 줄인다.
6. 새 자식 프로세스의 준비 신호와 초기 종료를 확인한다. 기동 실패면 이전 파일·프로세스로 되돌린다.
   인터넷 연결 여부 자체를 기동 성공 조건으로 삼지 않아 현장 오프라인을 업데이트 실패로 오판하지 않는다.
   불필요한 출력 pipe를 만들지 않고 종료/출력 대기를 제한한다. 실제 중지를 확인할 수 없으면 오류로 멈추고 교체·재기동을 진행하지 않는다.
7. 복원도 해시를 확인한 메모리 바이트를 원자 설치한다. 백업 경로를 다시 Rename하지 않는다.
   심볼릭 링크 백업의 대상을 바꾸면 복원 실행 경로도 바뀌던 문제를 독립 overlay 시험으로 재현하고,
   링크·해시 불일치 회귀시험을 추가했다. 최초 실패와 수정 후 통과는 `var/review-rollback-overlay*.log`에 보존했다.
8. Windows 버전 조회와 워커는 별도 콘솔 없이 실행한다. 감독 프로세스가 전달하는 입출력 핸들과 준비 파일로 통신한다.
   운영 서비스와 백그라운드 CLI 모두에 적용한다. [Windows 프로세스 생성 플래그](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags).

## 결과

허용 목록 밖 라이선스나 모델은 추가하지 않는다. 설정이 꺼져 있으면 갱신 네트워크 요청이 없어야 한다.
PoC는 운영 서비스 설치 검증을 대신하지 않는다. Windows 서비스 권한·현장 설치, 키 배포·운영 URL은 실제 운영 입력으로 확인한다.
06번의 코드·시험·문서가 모두 끝나기 전에는 자동 업데이트 작업을 완료로 표시하지 않는다.

### 2026-11-05 Windows 재검증

처음에는 `version`과 워커 실행에서 CPU 0·단일 Initialized 스레드 상태가 관찰됐다. 후속 Go 시험 스택과 독립
프로브는 지연 구간이 Windows `CreateProcess` 호출임을 확인했다. 호출 반환 뒤에 워커 준비 제한 시간이 적용된다.
지연과 함께 콘솔 프로세스가 생기는 사례가 있어 불필요한 콘솔을 제거했다. 최종 제품 CLI 시험에서도 초기화 지연이
한 차례 관찰됐다. OS 호출 지연의 정확한 원인은 확인 필요다.

- 실제 Windows 업데이트 시험 14개가 28.1초에 통과했다. 합성 워커의 시작 기록과 감독의 준비 완료를 기다려,
  설치 전 실패를 복원 성공으로 세거나 준비 전 자동 시험을 종료하는 거짓 통과를 막았다. 시험의 Windows 시간 여유는
  20초(버전/통신)·12초(준비)이며 제품 설정 기본값과 별개다. `var/update-no-console-windows-native.log`.
- 최종 제품 Windows CLI에서 합성 등록, 서명 갱신 1.0.0 → 2.0.0, 롤백 1.0.0을 66.1초에 검증했다.
  설치·복원 바이너리 SHA-256이 일치했고 DPAPI 자격증명과 감시 원본을 그대로 보존했다.
  `var/update-cli-nowindow-20261105-151357-30068/result.json`, `checks.json`.
- Linux 업데이트 15개 race와 Linux/Windows 대상 vet를 통과했다. `var/update-no-console-linux-race.log`.
- 이전 실패와 재현은 `var/update-windows-native.log`, `var/update-cli-windows-native.log`,
  `var/update-ready-windows-native.log`, `var/update-createprocess-probe.jsonl`에 보존했다.

작업 15·16의 로컬 검증을 완료했다. 실제 서비스 설치·로그온·부팅은 현장 확인 범위다.
