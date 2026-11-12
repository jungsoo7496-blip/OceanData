# ADR-0068: 요청 본문 크기 한도를 웹 프록시와 같은 값으로 앱에도 둔다

- 상태: accepted
- 날짜: 2026-11-12
- 관련: 01번 요구사항 4(요청 추적)·8(지표)·10(오류 형식), ADR-0056(검증기 문제 목록 상한·좌표/중첩 가드), ADR-0062(DB 작업 차례), api-conventions.md 오류 코드 표, 검토 2026-11-12 V5·사람이 정할 것 5번

## 맥락

검토(V5)는 "문제 목록이 무한하고 앱에 요청 본문 크기 제한이 없다"를 재현했다(20KB 요청 → 2.6MB 응답, 0.4MB 좌표 → 80MB 응답·260MB 메모리). 문제 목록 100개 상한과 좌표·중첩 가드는 ADR-0056에서 고쳤지만
본문 크기 제한은 웹 프록시(`web/nginx.conf`의 `/v1/`에 `client_max_body_size 20m`)에만 있었다.
프록시를 거치지 않는 요청 — Compose는 API 포트 8000을 바로 열고, 에이전트는 API 주소로 바로 보낸다(개발 안내의 `--server http://localhost:8000`) — 은 크기와 상관없이 본문을 끝까지 메모리에 읽은 뒤 해석했다.
`Content-Length` 없이(chunked) 보내면 끝 표시가 올 때까지 기다렸다.

검토 문서의 '사람이 정할 것' 5번(요청 본문 크기 제한을 앱 미들웨어에 둘지 프록시에만 둘지)은 아직 답이 없다.

## 결정

**이 결정은 소유자의 답을 기다리는 동안 밤 작업 조정자(Claude)가 택한 보수적이고 되돌릴 수 있는 선택이다.** 프록시 한도는 그대로 두고 같은 값의 한도를 앱에도 둔다(심층 방어).
기본값이 프록시와 같아서 오늘 프록시가 받는 요청은 앱도 받는다. 더 작은 값이 필요한지는 사람이 정한다.

1. 순수 ASGI 미들웨어 `RequestBodyLimitMiddleware`(`app/middleware.py`)를 `RequestContextMiddleware` **안쪽**에 둔다(`main.py`, 나중에 더한 것이 바깥).
   거절한 요청에도 `X-Request-Id`, 지표(`http_requests_total`·`http_errors_total`·지연), `http_request` 로그가 남고, 따로 `request_body_too_large` 경고 로그(경로·한도·Content-Length)를 남긴다.
2. **`Content-Length`가 한도를 넘으면 본문을 읽지 않고 앱을 부르지 않은 채 413**으로 답한다. 경로·인증·DB 작업 차례(ADR-0062)·연결을 쓰지 않는다.
3. **`Content-Length`가 없으면(chunked) 받는 조각의 길이만 더하다가 한도를 넘는 순간 `RequestBodyTooLarge`를 던지고 더 읽지 않는다.** 본문을 모아 두지 않는다(받은 조각은 그대로 넘긴다).
   FastAPI는 의존성보다 먼저 본문을 읽으므로 DB 작업 차례를 받기 전이다. `RequestBodyTooLarge`는 Starlette `HTTPException`(413)을 잇는다 — FastAPI의 본문 해석은 다른 예외를 400 "본문 해석 오류"로 바꾸고
   `HTTPException`만 그대로 다시 던지기 때문이다. 오류 처리기(`errors.py`)가 problem+json으로 답하고, 경로 밖에서 본문을 읽다 올라온 경우도 미들웨어가 413으로 답한다(500이 되지 않는다).
4. 응답은 RFC 9457 problem+json, 413 `PAYLOAD_TOO_LARGE`, `detail`에 한도 바이트 수. 한도와 같은 크기는 받는다(nginx도 넘을 때만 거절한다). 숫자가 아닌 `Content-Length`는 무시하고 세기로 판단한다.
5. 설정 `MAX_REQUEST_BODY_BYTES`, 기본 20,971,520(nginx `20m`, nginx의 `m`은 1024²). 빈 값(`KEY=`)은 기본값이다('한도 없음'이 아니다). 0·음수·숫자가 아닌 값은 키 이름만 출력하고 기동을 멈춘다.
   `.env.example`·Compose(`x-app-env`)에 같은 기본값으로 넣었고, `web/nginx.conf`에 두 값을 함께 보라는 주석을 달았다.
6. 계약 우선: `contracts/openapi.yaml` 머리말에 "어느 작업이든 본문 한도를 넘으면 413 `PAYLOAD_TOO_LARGE`를 줄 수 있다" 한 줄(설명만), api-conventions.md 오류 표의 `PAYLOAD_TOO_LARGE` 줄에 같은 내용을 더했다.

작업별 한도 확인: 본문을 받는 경로는 FastAPI 본문(JSON·폼·바이트)뿐이고 `request.body()`·`request.stream()`을 직접 부르는 곳은 없다. 작업별 한도는 HWPX 양식 PUT 10MiB(계약 `maxLength: 10485760`),
관측값 배치 항목 수(기본 5,000, 스키마 검증 전 413), 토큰 폼 필드 길이, 검증기 문제 목록·좌표·중첩 가드(ADR-0056)이며 모두 20MiB 아래라 전과 같이 동작한다(HWPX 10MiB+1은 여전히 경로가 413으로 답한다).
큰 바이너리는 사전 서명 URL로 저장소에 바로 가므로 한도에서 뺄 경로가 없다.

버린 대안:
- 프록시에만 둔다(지금까지) — 프록시를 거치지 않는 직접 요청이 막히지 않는다.
- 앱 한도를 프록시보다 작게(예: 1MiB) — 지금 받는 요청 중 무엇이 거절될지 확인하지 않았다(관측값 배치·업로드 목록 크기). 사람이 정할 값이다.
- uvicorn 옵션 — 설치된 uvicorn 0.54.0 설정에는 HTTP 본문 크기 한도가 없다(`--limit-concurrency`는 동시 요청 수이고 응답이 problem+json이 아니다).
- 경로마다 한도 — 경로가 많아 빠뜨리기 쉽고, 본문을 다 읽은 뒤에야 크기를 알 수 있다(메모리를 이미 쓴 뒤다).

## 결과

좋아지는 점: 프록시를 거치지 않아도 큰 본문은 메모리·CPU를 쓰기 전에 413이다. chunked 본문도 끝을 기다리지 않는다. 거절한 요청은 DB 작업 차례·연결을 쓰지 않는다. 기본값이 프록시와 같아 지금 받는 요청은 그대로다.

감수할 점:
- 한도가 두 곳(프록시·앱)에 있어 함께 바꿔야 한다. 프록시가 먼저 거절하면 응답 형식은 프록시가 정한다(지금과 같다).
- `Content-Length`로 거절한 요청은 경로를 찾기 전이라 지표의 route 라벨이 `unmatched`다(경로는 경고 로그에 남는다). chunked로 거절한 요청은 경로 이름으로 남는다.
- 거절한 뒤 남은 본문을 어떻게 버릴지(연결을 닫을지 등)는 서버(uvicorn)가 정한다. 앱은 더 읽지 않는다.
- 413을 경로의 한도가 냈는지 전체 한도가 냈는지는 `detail`(전체 한도는 바이트 수를 적는다)과 경고 로그로 구분한다. 코드는 같은 `PAYLOAD_TOO_LARGE`다.

되돌리는 방법: 설정만으로는 `MAX_REQUEST_BODY_BYTES`를 아주 크게 두면 사실상 꺼진다. 코드로는 `main.py`의 `add_middleware(RequestBodyLimitMiddleware, …)` 한 줄을 지우면 전처럼 프록시에만 한도가 있다.
소유자가 "프록시에만"으로 정하면 그 줄과 `RequestBodyLimitMiddleware`·`RequestBodyTooLarge`(오류 처리기 포함)·설정·`.env.example`·Compose 줄·계약 머리말 한 줄·api-conventions.md 문구·`nginx.conf` 주석을 지운다. 마이그레이션 없음.

시험: `tests/unit/test_request_body_limit.py` — Content-Length 초과는 경로가 돌기 전에 413(요청 ID가 헤더·본문에 같다), 한도와 같은 크기·작은 본문·본문 없음은 그대로, Content-Length 없는 본문도 세어 413,
본문을 받는 경로(검증 JSON·토큰 폼)는 DB 작업 차례를 받지 않음, 끝나지 않는 본문은 한도를 넘은 순간 그만 읽음(receive 3번), 숫자가 아닌 Content-Length, 기본값이 `web/nginx.conf`와 같음,
빈 값은 기본값, 0·음수·문자는 기동 실패, `.env.example`·Compose 배선. `tests/integration/test_request_body_limit_api.py` — 실제 uvicorn과 원시 소켓으로 끝 표시 없는 chunked 6KiB(한도 4KiB)가
끝을 기다리지 않고 413·DB 차례 0·지표 +1, Content-Length 초과는 인증 없이도 413, 한도와 같은 본문은 평소처럼 200(Content-Length·chunked 둘 다), HWPX 10MiB+1은 경로 한도가 그대로 413.
수정 전 코드에서 새 시험 14개가 실패하는 것을 확인했다(chunked 시험은 10초 동안 응답이 없어 실패). 고친 뒤 `main.py`의 미들웨어 줄만 빼도 HTTP 수준 시험 5개가 다시 실패한다.
