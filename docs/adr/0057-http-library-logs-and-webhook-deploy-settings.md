# ADR-0057: HTTP 라이브러리 로그는 WARNING 이상만, 웹훅 전송 설정은 워커에만

- 상태: accepted
- 날짜: 2026-11-12
- 관련: 01번 요구사항 4, 04번 요구사항 8, security.md, ADR-0041(웹훅 전송), ADR-0050(운영자 알림 웹훅), 지시서 12번 웹훅 검토 F2·F6

## 맥락

- `httpx`는 요청마다 `HTTP Request: POST <전체 URL> "HTTP/1.1 204"`를 INFO로 남긴다. 로그 정리기(`scrub`)는 URL의 **쿼리·조각만** 지운다. Slack·Teams·Discord 방식 웹훅 URL은 토큰이
  **경로**에 있어(`/services/T000/B000/XXXX`) 기관 웹훅 전송(고정 IP로 바꾼 URL 포함)과 운영자 알림(`ALERT_WEBHOOK_URL`)마다 토큰이 로그에 남았다(기본 `LOG_LEVEL=INFO`). security.md는 토큰을 로그에 남기지 말라고 한다.
- `ALERT_WEBHOOK_SECRET`(서명), `WEBHOOK_DISPATCH_INTERVAL_S`, `WEBHOOK_BACKOFF_BASE_S`, `WEBHOOK_BACKOFF_MAX_S`가 `Settings`에는 있는데 Compose와 `.env.example`에 없어서 배포에서 켤 수 없었다
  (ADR-0050의 "비밀이 있으면 서명한다"에 도달할 수 없었다).

## 결정

1. `configure_logging`이 `httpx`·`httpcore` 로거를 WARNING으로 올린다(루트 수준과 무관하게 — `LOG_LEVEL=DEBUG`에서도 요청 줄은 남기지 않는다). 앱 로그의 수준은 그대로다.
   경로 토큰을 정리기로 가리는 대신 요청 줄 자체를 남기지 않는다: 가릴 부분을 URL마다 판단할 수 없고, 이 줄은 앱의 구조 로그(`http_request`, 요청 ID 포함)와 겹친다.
2. 위 네 설정을 `.env.example`에 적고 Compose의 **워커 서비스에만** 넘긴다. 전송은 워커의 전용 스레드가 하고(ADR-0041·0050), API 컨테이너가 서명 비밀을 가질 이유가 없다.
   `ALERT_WEBHOOK_URL`은 이미 공용 환경(`x-app-env`)에 있어 그대로 둔다(CLI `watch`도 쓴다).

## 결과

좋아지는 점: 웹훅 경로 토큰이 로그에 남지 않는다. 운영자 알림 서명과 전송 간격이 배포 설정으로 닿는다. 서명 비밀이 API 컨테이너로 퍼지지 않는다.

감수할 점: 외부 HTTP 호출의 디버그 추적에 라이브러리 로그를 쓸 수 없다(앱 로그와 메트릭으로 본다). `ALERT_WEBHOOK_URL`은 여전히 API 컨테이너 환경에도 있다.

되돌리는 방법: `configure_logging`의 두 줄을 지운다. 배포 설정 항목은 기본값이 코드와 같아 지워도 동작이 같다.

시험: `tests/unit/test_webhook_url_not_logged.py`(INFO·DEBUG 모두 경로 토큰 없음, 앱 DEBUG 로그는 남음). 수정 전 코드에서 2개 모두 실패하는 것을 확인했다.
