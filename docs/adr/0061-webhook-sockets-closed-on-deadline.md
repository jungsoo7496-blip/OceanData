# ADR-0061: 웹훅 전송은 연 TCP 연결을 어떻게 끝나든 직접 닫는다

- 상태: accepted
- 날짜: 2026-11-12
- 관련: 04번 요구사항 8.2·8.3(웹훅 전송), ADR-0041(전송 격리·총 기한·주소 고정), 지시서 12번 웹훅 검토 W3

## 맥락

웹훅 한 건 전송(`webhooks.http_send`)은 이름 풀기부터 응답 헤더까지 전체를 `asyncio.wait_for`의 기한 안에 끝낸다(ADR-0041). 기한이 TLS 핸드셰이크 도중에 닿으면 전송 코루틴이
취소(`CancelledError`)로 끝나는데, httpcore 1.0.9의 TLS 시작 코드는 `except Exception`일 때만 TCP 스트림을 닫는다. `CancelledError`는 `BaseException`이라 이 경로를 타지 않고,
연결 풀에도 아직 올라가지 않은 스트림이라 클라이언트를 닫아도 닫히지 않는다. 그 소켓은 가비지 수집 때까지 열려 있었다(검토 재현: 수집을 끄고 20회 시도에 소켓 20개).
TLS 핸드셰이크에서 멈추는 수신 서버(악의든 장애든) 하나가 워커의 파일 기술자를 수집 주기 사이에 쌓이게 한다.

## 결정

1. **전송마다 연 TCP 연결을 기록하는 네트워크 백엔드(`_ClosingBackend`)를 쓰고, `_post`의 `finally`에서 모두 닫는다.** 정상 종료로 이미 닫힌 것은 그대로 지나가므로 정상 경로의 동작은 같다.
   안쪽은 httpcore의 `AnyIOBackend`를 그대로 쓴다(연결·TLS·타임아웃 동작 동일). 유닉스 소켓은 웹훅에 쓰지 않으므로 거부한다.
2. httpx 0.28은 네트워크 백엔드를 받는 공개 인자가 없어 `AsyncHTTPTransport`의 연결 풀(`_pool._network_backend`)을 바꾼다. 이름이 없어지면 조용히 넘어가지 않고 `RuntimeError`로 실패한다.
   httpx·httpcore를 올릴 때 `tests/unit/test_webhook_send.py`의 소켓 닫힘 시험이 이를 잡는다.
3. httpcore를 `backend/pyproject.toml`에 직접 의존성으로 적는다(`>=1.0,<2`). httpx가 이미 끌어오던 것(BSD-3-Clause)이라 설치되는 패키지와 라이선스 검사 대상은 바뀌지 않는다.

버린 대안: 연결 단계 timeout을 남은 시간보다 짧게 잡아 취소 대신 시간 초과 예외로 끝나게 하기 — TCP 연결과 TLS 시작이 각각 같은 connect timeout을 쓰고 주소가 여럿이면 다음 주소로 넘어가,
총 기한보다 먼저 끝난다는 보장이 없다. 시간 초과 뒤 `gc.collect()` 부르기 — 큰 프로세스에서 비용이 크고 원인을 고치지 않는다. httpx 전송 계층을 직접 구현하기 — 요청·응답 변환과 예외 매핑을 다시 써야 한다.

## 결과

좋아지는 점: 기한으로 끊긴 전송의 소켓이 바로 닫혀 서버도 EOF를 받는다. 수신 서버가 TLS에서 멈춰도 워커의 파일 기술자가 쌓이지 않는다.

감수할 점: httpx의 내부 이름(`_pool._network_backend`)에 기댄다 — 올릴 때 시험이 잡는다. TCP 연결 단계 자체가 취소되는 경우는 anyio가 정리하며 이번 시험 범위 밖이다.
1xx 응답 범람의 CPU 사용(W4)과 라운드 장벽(W1)은 따로다.

되돌리는 방법: `_post`에서 `transport=` 인자와 `finally`를 지우고 `httpx.AsyncClient(verify=…)`로 되돌린다. `_ClosingBackend`·`_transport`와 pyproject의 httpcore 줄을 지운다. 계약·마이그레이션 변경 없음.

시험: `tests/unit/test_webhook_send.py::test_socket_is_closed_when_deadline_hits_during_tls_handshake` — TCP는 받고 TLS에 답하지 않는 합성 서버로 0.3초 기한 전송을 3번 하고, 가비지 수집을 끈 채
서버가 2초 안에 EOF를 3번 받는지 본다. 수정 전 코드에서 0번으로 실패하는 것을 확인했다.
