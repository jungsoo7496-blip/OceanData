"""운영자 웹훅의 전체 기한·응답 본문 미수신·기존 프록시와 서명."""

from __future__ import annotations

import hashlib
import hmac
import socket
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue
from threading import Event, Thread

import pytest

from app.services import monitoring

NOW = datetime(2026, 11, 7, tzinfo=UTC)
PAYLOAD = {"type": "watch.opened", "summary": "합성 경보"}
SECRET = "synthetic-fixture-key"
WireRequest = tuple[str, dict[str, str], bytes]


@pytest.fixture(autouse=True)
def _only_local_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)


@contextmanager
def _receiver(respond: Callable[[BaseHTTPRequestHandler], None]) -> Iterator[tuple[str, Queue[WireRequest]]]:
    requests: Queue[WireRequest] = Queue()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            headers = {key.lower(): value for key, value in self.headers.items()}
            body = self.rfile.read(int(headers["content-length"]))
            requests.put((self.path, headers, body))
            with suppress(OSError):  # 응답을 읽지 않고 연결을 닫는 경우
                respond(self)

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        serving = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        serving.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", requests
        finally:
            server.shutdown()
            serving.join(timeout=2)


def _respond_204(handler: BaseHTTPRequestHandler) -> None:
    handler.wfile.write(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")


def test_operator_webhook_returns_while_response_body_is_held(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(monitoring, "_ALERT_WEBHOOK_TIMEOUT_S", 3.0, raising=False)
    headers_sent, release = Event(), Event()

    def hold_body(handler: BaseHTTPRequestHandler) -> None:
        handler.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nConnection: close\r\n\r\n")
        headers_sent.set()
        release.wait(6)
        handler.wfile.write(b"x")

    with _receiver(hold_body) as (base, _), ThreadPoolExecutor(max_workers=1) as pool:
        posting = pool.submit(monitoring.post_webhook, f"{base}/ops", None, PAYLOAD, NOW)
        try:
            assert headers_sent.wait(4), "수신기가 응답 헤더를 보내지 못했습니다"
            posting.result(timeout=1.25)  # 본문을 보내거나 전체 기한이 끝나기 전에 복귀해야 한다.
        finally:
            release.set()


def test_operator_webhook_cuts_trickling_incomplete_headers_at_total_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(monitoring, "_ALERT_WEBHOOK_TIMEOUT_S", 0.45, raising=False)
    header_started, release = Event(), Event()

    def drip_headers(handler: BaseHTTPRequestHandler) -> None:
        handler.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
        header_started.set()
        for _ in range(50):
            if release.wait(0.08):
                break
            handler.wfile.write(b"x")
        handler.wfile.write(b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")

    with _receiver(drip_headers) as (base, _), ThreadPoolExecutor(max_workers=1) as pool:
        posting = pool.submit(monitoring.post_webhook, f"{base}/ops", None, PAYLOAD, NOW)
        try:
            assert header_started.wait(4), "수신기가 응답 헤더를 시작하지 못했습니다"
            posting.result(timeout=1.5)  # 바이트가 계속 들어와도 완성되지 않은 헤더의 총 기한은 유한하다.
        finally:
            release.set()


def test_operator_webhook_deadline_includes_stalled_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(monitoring, "_ALERT_WEBHOOK_TIMEOUT_S", 0.45, raising=False)
    host = "operator-probe.invalid"
    entered, release, exited = Event(), Event(), Event()
    original_lookup = socket.getaddrinfo

    def hold_dns(name, *args, **kwargs):
        if name not in (host, host.encode()):
            return original_lookup(name, *args, **kwargs)
        entered.set()
        try:
            assert release.wait(6), "DNS 시험 정리 시간이 지났습니다"
            return original_lookup("127.0.0.1", *args, **kwargs)
        finally:
            exited.set()

    monkeypatch.setattr(socket, "getaddrinfo", hold_dns)
    with _receiver(_respond_204) as (base, _), ThreadPoolExecutor(max_workers=1) as pool:
        port = base.rsplit(":", 1)[-1]
        posting = pool.submit(monitoring.post_webhook, f"http://{host}:{port}/ops", None, PAYLOAD, NOW)
        try:
            assert entered.wait(4), "웹훅이 합성 DNS 조회를 시작하지 못했습니다"
            posting.result(timeout=1.5)
            assert not exited.is_set()
        finally:
            release.set()
            if entered.is_set():
                assert exited.wait(3), "합성 DNS 조회가 정리되지 않았습니다"


def test_operator_webhook_preserves_signed_wire_payload_through_environment_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _receiver(_respond_204) as (proxy, requests):
        monkeypatch.setenv("http_proxy", proxy)
        target = f"{proxy}/ops?source=fixture"
        monitoring.post_webhook(target, SECRET, PAYLOAD, NOW)
        path, headers, body = requests.get(timeout=2)

    assert path == target  # HTTP 프록시에서는 원래 URL 전체가 요청 경로다.
    assert body == '{"type":"watch.opened","summary":"합성 경보"}'.encode()
    stamp = str(int(NOW.timestamp()))
    signature = hmac.new(SECRET.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    assert headers["content-type"] == "application/json"
    assert headers["x-signature-timestamp"] == stamp
    assert headers["x-signature"] == f"sha256={signature}"
