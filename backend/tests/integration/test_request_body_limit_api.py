"""앱 수준 요청 본문 한도를 실제 앱·DB로 확인한다 (검토 2026-11-12 V5, ADR-0068).

본문 크기 제한이 웹 프록시에만 있어, API 포트로 바로 오는 요청은 본문을 끝까지 읽은 뒤 처리했다.
Content-Length 없이(chunked) 오는 본문은 실제 uvicorn과 원시 소켓으로 보낸다 — 끝 표시(0 청크)를 보내지 않아도
한도를 넘는 순간 413이 와야 한다(본문 끝을 기다리지 않는다).
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from typing import Any

import pytest

from app.metrics import REGISTRY
from tests import conftest
from tests.conftest import Ctx, FakeStorage, StaticIdentity
from tests.integration.test_request_burst import _Live

pytestmark = pytest.mark.integration

LIMIT = 4096
VALIDATE = "/v1/contracts/validate"
JSON = {"Content-Type": "application/json"}


class _Limited:
    def __init__(self, app: Any, holds: list[int]) -> None:
        self.app, self.holds = app, holds


@pytest.fixture()
def limited(ctx: Ctx, db_url: str) -> Iterator[_Limited]:
    """본문 한도를 4KiB로 줄인 앱. ctx가 시드한 DB를 같이 쓰고, DB 작업 차례(ADR-0062)를 받은 횟수를 센다."""
    from app.adapters.notifier import LogNotifier
    from app.container import build_container
    from app.main import create_app

    settings = conftest.test_settings(db_url, max_request_body_bytes=LIMIT)
    container = build_container(settings, storage=FakeStorage(), identity=StaticIdentity(), notifier=LogNotifier())
    holds: list[int] = []
    inner = container.db_slots.hold
    container.db_slots.hold = lambda: (holds.append(1), inner())[1]  # type: ignore[method-assign]
    yield _Limited(create_app(container), holds)
    container.engine.dispose()


def _validate_body(size: int) -> bytes:
    """크기를 정확히 맞춘 정상 검증 요청 (JSON 뒤 공백으로 채운다)."""
    raw = json.dumps({"contract": "event", "document": {}}).encode()
    assert len(raw) <= size
    return raw + b" " * (size - len(raw))


def _requests_413() -> float:
    total = 0.0
    for metric in REGISTRY.collect():
        for s in metric.samples:
            if s.name == "http_requests_total" and s.labels.get("status") == "413":
                total += s.value
    return total


def _assert_413(status: int, headers: dict[str, str], body: dict) -> None:
    assert status == 413, body
    assert headers["content-type"].startswith("application/problem+json")
    assert body["code"] == "PAYLOAD_TOO_LARGE" and body["instance"] == VALIDATE
    assert body["request_id"] == headers["x-request-id"]


def _read_response(s: socket.socket) -> tuple[int, dict[str, str], dict]:
    raw = b""
    while b"\r\n\r\n" not in raw:
        chunk = s.recv(65536)
        assert chunk, "응답 없이 연결이 닫혔다"
        raw += chunk
    head, _, rest = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
    length = int(headers["content-length"])
    while len(rest) < length:
        chunk = s.recv(65536)
        assert chunk, "응답 본문을 다 받기 전에 연결이 닫혔다"
        rest += chunk
    return int(lines[0].split(" ", 2)[1]), headers, json.loads(rest[:length])


def test_chunked_body_over_limit_is_rejected_without_waiting_for_the_end(ctx: Ctx, limited: _Limited) -> None:
    before = _requests_413()
    auth = ctx.as_user(102)["Authorization"]
    with _Live(limited.app) as port:
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        try:
            s.sendall(f"POST {VALIDATE} HTTP/1.1\r\nHost: x\r\nAuthorization: {auth}\r\n"
                      "Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n".encode())
            for _ in range(6):  # 6KiB > 4KiB. 끝 표시(0 청크)는 보내지 않는다
                s.sendall(b"400\r\n" + b" " * 1024 + b"\r\n")
            try:
                status, headers, body = _read_response(s)
            except TimeoutError:
                pytest.fail("한도를 넘은 본문의 끝을 기다렸다 (10초 안에 응답 없음)")
        finally:
            s.close()
    _assert_413(status, headers, body)
    assert limited.holds == []  # DB 작업 차례·연결을 쓰지 않았다
    assert _requests_413() == before + 1  # 거절한 요청도 지표에 남는다


def test_content_length_over_limit_is_rejected_before_auth_and_db(ctx: Ctx, limited: _Limited) -> None:
    from fastapi.testclient import TestClient

    before = _requests_413()
    with TestClient(limited.app, raise_server_exceptions=False) as client:
        r = client.post(VALIDATE, content=_validate_body(LIMIT + 1), headers={**JSON, **ctx.as_user(102)})
        _assert_413(r.status_code, dict(r.headers), r.json())
        r = client.post(VALIDATE, content=_validate_body(LIMIT + 1), headers=JSON)  # 인증 없이도 같은 413
        _assert_413(r.status_code, dict(r.headers), r.json())
    assert limited.holds == []
    assert _requests_413() == before + 2


def test_body_at_limit_is_processed_as_before(ctx: Ctx, limited: _Limited) -> None:
    from fastapi.testclient import TestClient

    h = {**JSON, **ctx.as_user(102)}
    body = _validate_body(LIMIT)
    with TestClient(limited.app, raise_server_exceptions=False) as client:
        r = client.post(VALIDATE, content=body, headers=h)
        assert r.status_code == 200 and r.json()["valid"] is False  # 한도와 같은 본문은 평소대로 검사한다
        r = client.post(VALIDATE, content=iter([body[:1000], body[1000:]]), headers=h)  # Content-Length 없이
        assert r.status_code == 200 and r.json()["valid"] is False
        assert client.post(VALIDATE, json={"contract": "event", "document": {}}, headers=h).status_code == 200
    assert len(limited.holds) == 3


def test_per_endpoint_limits_below_the_global_limit_still_apply(ctx: Ctx) -> None:
    """기본 한도(20MiB)보다 작은 경로별 한도는 그대로다: HWPX 양식 10MiB, 관측값 배치 항목 수."""
    op = ctx.as_user(106)
    r = ctx.client.put("/v1/report-templates/EXAMPLE_SESSION_LOG/versions/1/hwpx", content=b"x" * (10 * 1024**2 + 1),
                       headers={**op, "Content-Type": "application/octet-stream"})
    assert r.status_code == 413 and r.json()["code"] == "PAYLOAD_TOO_LARGE"
    assert "detail" not in r.json()  # 경로의 한도가 답했다 (전체 한도는 detail에 바이트 수를 적는다)
