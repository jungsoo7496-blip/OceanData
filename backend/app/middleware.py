"""요청 ID·로그 컨텍스트·메트릭 (01번 요구사항 4·8). 순수 ASGI 미들웨어."""

from __future__ import annotations

import logging
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.context import organization_id_var, request_id_var
from app.domain.ids import is_ulid, new_ulid
from app.metrics import HTTP_ERRORS, HTTP_LATENCY, HTTP_REQUESTS

log = logging.getLogger("http")


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

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", rid.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
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
