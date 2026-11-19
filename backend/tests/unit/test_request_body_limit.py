"""앱 수준 요청 본문 한도 (검토 2026-11-12 V5, ADR-0068): 미들웨어 동작과 설정.

본문 크기 제한이 웹 프록시(`web/nginx.conf`의 `client_max_body_size 20m`)에만 있어, 프록시를 거치지 않고 API 포트로
바로 오는 요청(에이전트·기관 서버)은 크기 제한 없이 본문을 끝까지 읽었다. DB는 쓰지 않는다(닿지 않는 주소).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.config import ConfigError
from app.domain.ids import is_ulid
from tests import conftest
from tests.conftest import FakeStorage, StaticIdentity

LIMIT = 100
REPO = Path(__file__).resolve().parents[3]


def _container(limit: int = LIMIT) -> Any:
    from app.container import build_container

    return build_container(conftest.test_settings(max_request_body_bytes=limit), storage=FakeStorage(),
                           identity=StaticIdentity())


def _client(limit: int = LIMIT) -> tuple[TestClient, list[int]]:
    """한도를 줄인 앱 + 본문을 끝까지 읽는 시험용 경로. 경로가 돈 횟수를 함께 돌려준다."""
    from app.main import create_app

    app: FastAPI = create_app(_container(limit))
    ran: list[int] = []

    @app.post("/__echo")
    async def echo(request: Request) -> dict:
        n = len(await request.body())
        ran.append(n)
        return {"n": n}

    return TestClient(app, raise_server_exceptions=False), ran


def _chunks(total: int, size: int = 30) -> Iterator[bytes]:
    """Content-Length 없이(Transfer-Encoding: chunked) 보내는 본문."""
    while total > 0:
        yield b"x" * min(size, total)
        total -= size


def _assert_413(r: Any, path: str) -> None:
    assert r.status_code == 413, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["code"] == "PAYLOAD_TOO_LARGE" and body["status"] == 413 and body["instance"] == path
    assert is_ulid(r.headers["x-request-id"]) and body["request_id"] == r.headers["x-request-id"]


def test_content_length_over_limit_is_rejected_before_the_route_runs() -> None:
    client, ran = _client()
    r = client.post("/__echo", content=b"x" * (LIMIT + 1))
    _assert_413(r, "/__echo")
    assert ran == []  # 본문을 읽기 전에 거절했다


def test_body_at_limit_and_small_bodies_are_unaffected() -> None:
    client, ran = _client()
    assert client.post("/__echo", content=b"x" * LIMIT).json() == {"n": LIMIT}  # 한도와 같으면 받는다
    assert client.post("/__echo", content=b"{}").json() == {"n": 2}
    assert client.post("/__echo").json() == {"n": 0}
    assert client.get("/healthz").status_code == 200
    assert ran == [LIMIT, 2, 0]


def test_body_without_content_length_is_counted_while_reading() -> None:
    client, ran = _client()
    r = client.post("/__echo", content=_chunks(LIMIT + 1))
    assert "content-length" not in {k.lower() for k in r.request.headers}
    _assert_413(r, "/__echo")
    assert ran == []
    assert client.post("/__echo", content=_chunks(LIMIT)).json() == {"n": LIMIT}


def test_rejected_body_route_takes_no_db_slot() -> None:
    """본문을 받는 경로(FastAPI가 의존성보다 먼저 본문을 읽는다)도 DB 작업 차례(ADR-0062)를 받기 전에 거절한다."""
    from app.main import create_app

    c = _container()
    holds: list[int] = []
    inner = c.db_slots.hold
    c.db_slots.hold = lambda: (holds.append(1), inner())[1]  # type: ignore[method-assign]
    client = TestClient(create_app(c), raise_server_exceptions=False)
    big = b'{"contract": "event", "document": {}}' + b" " * LIMIT
    _assert_413(client.post("/v1/contracts/validate", content=big, headers={"Content-Type": "application/json"}),
                "/v1/contracts/validate")
    _assert_413(client.post("/v1/contracts/validate", content=_chunks(len(big)),
                            headers={"Content-Type": "application/json"}), "/v1/contracts/validate")
    form = b"grant_type=client_credentials&client_id=c&client_secret=" + b"s" * LIMIT  # 폼 본문도 같다
    _assert_413(client.post("/v1/auth/token", content=iter([form[:50], form[50:]]),
                            headers={"Content-Type": "application/x-www-form-urlencoded"}), "/v1/auth/token")
    assert holds == []
    c.engine.dispose()


def test_middleware_stops_reading_once_over_limit() -> None:
    """Content-Length가 없으면 읽은 만큼 세다가 한도를 넘는 순간 더 읽지 않는다 (본문 전체를 버퍼에 쌓지 않는다)."""
    from starlette.requests import Request as StarletteRequest

    from app.middleware import RequestBodyLimitMiddleware

    calls = 0

    async def receive() -> dict:
        nonlocal calls
        calls += 1
        assert calls < 50, "한도를 넘은 뒤에도 본문을 계속 읽었다"
        return {"type": "http.request", "body": b"x" * 40, "more_body": True}  # 끝나지 않는 본문

    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    async def app(scope: dict, receive: Any, send: Any) -> None:
        await StarletteRequest(scope, receive).body()
        raise AssertionError("본문을 끝까지 읽었다")

    scope = {"type": "http", "method": "POST", "path": "/x", "headers": [], "query_string": b""}
    anyio.run(RequestBodyLimitMiddleware(app, max_bytes=LIMIT), scope, receive, send)
    assert calls == 3  # 40 → 80 → 120(한도 초과)에서 멈춘다
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 413


def test_invalid_content_length_falls_back_to_counting() -> None:
    from starlette.requests import Request as StarletteRequest
    from starlette.responses import PlainTextResponse

    from app.middleware import RequestBodyLimitMiddleware

    async def receive() -> dict:
        return {"type": "http.request", "body": b"x" * 10, "more_body": False}

    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    async def app(scope: dict, receive: Any, send: Any) -> None:
        n = len(await StarletteRequest(scope, receive).body())
        await PlainTextResponse(str(n))(scope, receive, send)

    scope = {"type": "http", "method": "POST", "path": "/x", "headers": [(b"content-length", b"abc")],
             "query_string": b""}
    anyio.run(RequestBodyLimitMiddleware(app, max_bytes=LIMIT), scope, receive, send)
    assert sent[0]["status"] == 200 and sent[1]["body"] == b"10"


# ---- 설정: 기본값은 웹 프록시 한도와 같다. 빈 값은 '설정 안 함'(기본값)


def _nginx_limit() -> int:
    text = (REPO / "web" / "nginx.conf").read_text(encoding="utf-8")
    m = re.search(r"client_max_body_size\s+(\d+)([kKmMgG]?)\s*;", text)
    assert m, "web/nginx.conf에 client_max_body_size가 없다"
    return int(m.group(1)) * {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[m.group(2).lower()]


def test_default_limit_mirrors_web_proxy() -> None:
    assert _nginx_limit() == 20 * 1024 * 1024
    assert conftest.test_settings().max_request_body_bytes == _nginx_limit()  # 오늘 프록시가 받는 요청은 앱도 받는다


@pytest.mark.parametrize("raw", ["", " "])
def test_empty_limit_means_default(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    assert conftest.test_settings(max_request_body_bytes=raw).max_request_body_bytes == 20 * 1024 * 1024
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", raw)
    assert conftest.test_settings().max_request_body_bytes == 20 * 1024 * 1024
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "1048576")
    assert conftest.test_settings().max_request_body_bytes == 1024 * 1024


@pytest.mark.parametrize("raw", ["0", "-1", "abc"])
def test_invalid_limit_stops_startup_with_key_name(raw: str) -> None:
    with pytest.raises(ConfigError) as e:
        conftest.test_settings(max_request_body_bytes=raw)
    assert e.value.invalid == ["MAX_REQUEST_BODY_BYTES"]


def test_limit_is_wired_for_deployment() -> None:
    """설정이 config.py에만 있으면 배포에서 바꿀 수 없다 (W6) — .env.example·Compose에 같은 기본값으로 둔다."""
    default = str(20 * 1024 * 1024)
    env = (REPO / ".env.example").read_text(encoding="utf-8")
    compose = (REPO / "deploy" / "compose" / "docker-compose.yml").read_text(encoding="utf-8")
    assert re.search(rf"^MAX_REQUEST_BODY_BYTES={default}$", env, re.M)
    assert f"MAX_REQUEST_BODY_BYTES: ${{MAX_REQUEST_BODY_BYTES:-{default}}}" in compose
