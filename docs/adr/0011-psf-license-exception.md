# ADR-0011: PSF-2.0 라이선스 의존성 예외 (승인 대기)

- 상태: 제안 — **사람 승인 필요** (tech.md 3장 "그 외는 사전 승인 없이 추가하지 않는다")
- 날짜: 2026-11-04
- 관련: 01번 요구사항 9.3, `tools/check_licenses.py`, `tools/license-exceptions.json`

## 맥락
라이선스 검사(`make license-check`)를 처음 돌려 보니 런타임 의존성 중 두 개가 허용 목록 밖이었다.

| 패키지 | 라이선스 | 끌어온 패키지 |
|---|---|---|
| typing_extensions | PSF-2.0 | FastAPI, Pydantic, SQLAlchemy |
| greenlet | MIT AND PSF-2.0 | SQLAlchemy |

tech.md 2장이 이미 고른 스택(FastAPI·Pydantic·SQLAlchemy)을 쓰면 피할 수 없다.

## 결정 (제안)
PSF-2.0을 허용 목록에 준하는 것으로 보고 예외로 둔다. PSF-2.0은 OSI 승인 허용형 라이선스로, CPython 자체와 같은 라이선스이며 카피레프트·소스 공개 의무가 없다. 저작권 고지만 THIRD_PARTY_NOTICES(17번)에 넣는다.

승인 전까지 검사는 경고만 출력하고 통과한다 (`approved: false`). 승인되면 `approved: true`로 바꾸거나 tech.md 3장 허용 목록에 PSF-2.0을 추가한다.

## 다른 선택지
- 스택 교체: tech.md의 결정을 뒤집어야 하고 다른 Python 웹 스택도 대부분 typing_extensions를 쓴다.
- 거부: 01번 이후 작업이 진행되지 않는다.

## 되돌리는 방법
`tools/license-exceptions.json`에서 항목을 지우면 검사가 실패한다.
