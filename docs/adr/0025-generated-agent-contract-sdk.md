# ADR-0025: 에이전트 계약 SDK의 재생성과 JSON Schema 참조 투영

- 상태: accepted
- 날짜: 2026-11-05
- 관련: 06번 작업 19, tech.md 계약 → 코드, ADR-0006

## 맥락

에이전트는 수동 응답 타입과 URL을 사용했다. 채택된 oapi-codegen의 수정판 v2.8.2로 실제 생성하면
외부 JSON Schema의 `#/$defs/...` 참조가 import mapping·참조 깊이 오류를 일으켰다.
원본의 OpenAPI 3.1·JSON Schema 2020-12 제약을 낮추거나 도메인 코드값을 바꾸지는 않는다.

## 결정

- oapi-codegen v2.8.2(Apache-2.0)를 버전 고정하고 생성 코드는 직접 수정하지 않는다.
  [수정판 근거](https://github.com/oapi-codegen/oapi-codegen/releases/tag/v2.8.2).
- `tools/bundle_agent_contract.py`는 로컬 참조 노드를 생성용 components/schemas로 옮긴다.
  조건·enum·required·null 허용 등 스키마 내용은 유지한다. 네트워크·계약 폴더 밖·동적 앵커 참조는 거부한다.
  소스 계약 자체를 고치지 않고 Windows/Linux에서 같은 이름을 생성한다.
- `tools/generate_agent_api.py`가 에이전트 사용 작업의 SDK를 생성하며 `--check`는 계약 변경 후 미재생성을 검출한다.
  발신 클라이언트만 생성하므로 외부 수신자용 `webhooks`는 생성 입력에서 제외한다. v2.8.2가
  미사용 웹훅 매개변수 타입만 제거해 컴파일할 수 없는 결과를 만드는 경로도 피한다. 원본 계약은 유지한다.
  생성 실행에만 Go와 기존 Python YAML 도구가 필요하다. 생성기 자체를 런타임에 링크하지 않는다.
- 런타임은 [oapi-codegen/runtime v1.6.0](https://github.com/oapi-codegen/runtime/tree/v1.6.0)을 사용한다.
  [LICENSE 원문](https://github.com/oapi-codegen/runtime/blob/v1.6.0/LICENSE)의 Apache-2.0을 확인했다.
  새 라이선스 예외는 추가하지 않는다. 실제 링크되는 전이 의존성도 검증한다.
  새로 링크된 `apapsch/go-jsonmerge/v2 v2.0.0`의 모듈 LICENSE(MIT)를 확인했다. `google/uuid`는 기존 BSD-3-Clause 의존성이다.
- 인증·재시도와 필드 누락을 검출하는 업무용 DTO는 기존 포트 어댑터에서 유지한다.
  생성된 영수증 요청 경로를 실제 어댑터에 연결하고 SDK 요청·응답을 합성 HTTP 시험으로 검증한다.
  생성 Go 타입만으로 JSON Schema의 모든 조건이 검증된다고 가정하지 않는다. 서버 계약 시험은 유지한다.

## 결과

API·DB·도메인 코드값 변경은 없으며 새 마이그레이션(down 포함)은 해당 없음이다.
스키마 투영 동등성·참조 경계·SDK 재생성·실제 HTTP 어댑터 검증이 통과한 뒤 작업 19를 완료로 표시한다.
