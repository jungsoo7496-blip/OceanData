"""요청 ID·로그 컨텍스트·메트릭 (01번 요구사항 4·8)과 요청 본문 한도 (ADR-0068). 순수 ASGI 미들웨어."""

from __future__ import annotations

import logging
import time

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.context import organization_id_var, request_id_var
from app.domain.ids import is_ulid, new_ulid
from app.errors import PROBLEM_JSON, RequestBodyTooLarge, body_limit_detail, instance_of, problem_body
from app.metrics import HTTP_ERRORS, HTTP_LATENCY, HTTP_REQUESTS

log = logging.getLogger("http")


def _content_length(scope: Scope) -> int | None:
    """Content-Length 헤더 값. 없거나 숫자가 아니면 None (그때는 읽는 만큼 센다)."""
    for k, v in scope.get("headers", []):
        if k == b"content-length":
            v = v.strip()
            return int(v) if v.isdigit() else None
    return None


class RequestBodyLimitMiddleware:
    """요청 본문이 max_bytes를 넘으면 413 PAYLOAD_TOO_LARGE (ADR-0068). 웹 프록시 한도와 별개로 앱에도 둔다.

    RequestContextMiddleware 안쪽에 둔다 — 거절한 요청에도 요청 ID·지표·로그가 남는다.
    - Content-Length가 한도를 넘으면 본문을 읽지 않고, 앱(경로·인증·DB 작업 차례)을 부르지 않고 답한다.
    - Content-Length가 없으면(chunked) 받는 만큼 길이를 더하다가 한도를 넘는 순간 RequestBodyTooLarge를 던지고
      더 읽지 않는다. FastAPI는 의존성보다 먼저 본문을 읽으므로 DB 작업 차례(ADR-0062)를 받기 전이다.
    - 본문을 모아 두지 않는다. 받은 조각을 그대로 넘기고 길이만 센다. 한도와 같은 크기는 받는다(nginx와 같다)."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = _content_length(scope)
        if declared is not None and declared > self.max_bytes:
            self._log(scope, declared)
            await self._reject(scope, receive, send)
            return
        received = 0
        started = False

        async def limited_receive() -> Message:
            nonlocal received
            if received > self.max_bytes:  # 이미 넘었다 — 더 읽지 않는다
                raise RequestBodyTooLarge(self.max_bytes)
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    self._log(scope, declared)
                    raise RequestBodyTooLarge(self.max_bytes)
            return message

        async def send_wrapper(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, send_wrapper)
        except RequestBodyTooLarge:
            # 보통은 경로의 예외 처리기(errors.py)가 413으로 답한다. 경로 밖에서 본문을 읽다 올라온 것도 413으로
            if started:
                raise
            await self._reject(scope, receive, send)

    def _log(self, scope: Scope, declared: int | None) -> None:
        log.warning("request_body_too_large", extra={"path": str(scope.get("path", ""))[:200],
                                                     "limit_bytes": self.max_bytes, "content_length": declared})

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        body = problem_body("PAYLOAD_TOO_LARGE", instance=instance_of(scope.get("path", "")),
                            detail=body_limit_detail(self.max_bytes))
        await JSONResponse(body, status_code=413, media_type=PROBLEM_JSON)(scope, receive, send)


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = None
        for k, v in scope.get("headers", []):
            if k == b"x-request-id":
                incoming = v.decode("latin-1").strip()
                break
        # 요청에 ULID가 있으면 그대로, 없거나 형식이 틀리면 새로 만든다 (로그 주입 방지)
        rid: str = incoming if incoming and is_ulid(incoming) else new_ulid()
        rid_token = request_id_var.set(rid)
        org_token = organization_id_var.set(None)
        start = time.perf_counter()
        status_holder = {"status": 500}

        started = {"v": False}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                started["v"] = True
                status_holder["status"] = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", rid.encode()))
                message["headers"] = headers
            await send(message)

        try:
            try:
                await self.app(scope, receive, send_wrapper)
            except Exception as exc:  # noqa: BLE001 - 처리 못 한 예외도 요청 ID를 단 문제 응답으로 (01번 요구사항 4)
                if started["v"]:
                    raise
                # 바깥 ServerErrorMiddleware까지 가면 요청 ID 문맥이 이미 풀려 request_id가 빈 값이 된다 (M8)
                log.error("unhandled_exception", exc_info=exc, extra={"path": scope.get("path")})
                body = problem_body("INTERNAL", instance=instance_of(scope.get("path", "")))
                await JSONResponse(body, status_code=500, media_type=PROBLEM_JSON)(scope, receive, send_wrapper)
        finally:
            route = scope.get("route")
            route_path = getattr(route, "path", None) or "unmatched"
            method = scope.get("method", "")
            status = status_holder["status"]
            elapsed = time.perf_counter() - start
            HTTP_REQUESTS.labels(method, route_path, str(status)).inc()
            HTTP_LATENCY.labels(method, route_path).observe(elapsed)
            if status >= 400:
                HTTP_ERRORS.labels(method, route_path, f"{status // 100}xx").inc()
            log.info("http_request", extra={"method": method, "route": route_path, "status": status,
                                            "duration_ms": round(elapsed * 1000, 2)})
            request_id_var.reset(rid_token)
            organization_id_var.reset(org_token)
