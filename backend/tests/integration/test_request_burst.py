"""요청 폭주에서 DB 연결 풀과 스레드 풀이 서로를 기다리지 않는다 (검토 2026-11-12 K8, ADR-0062).

동기 의존성·경로 함수는 요청마다 스레드 풀을 여러 번 오가는데(인증 → 권한 → 경로 → 커밋) 그동안 DB 연결을 쥐고 있다.
동시 요청이 스레드 한도(기본 40)보다 많으면 스레드는 연결을 기다리는 요청들이 차지하고, 연결은 다음 스레드 차례를
기다리는 요청들이 쥐어 서로 막힌다 — 연결 대기 시간(pool_timeout)이 지나 500이 났다. 풀을 스레드 한도보다 크게 해도
같았다(풀 60·요청 150에서 62건 500). 실제 uvicorn과 원시 소켓으로 확인한다.
"""

from __future__ import annotations

import socket
import threading
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
import uvicorn

from tests import conftest
from tests.conftest import Ctx, FakeStorage, StaticIdentity

pytestmark = pytest.mark.integration


def _get(port: int, path: str, headers: dict[str, str]) -> int:
    s = socket.create_connection(("127.0.0.1", port), timeout=60)
    try:
        head = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n{head}\r\n".encode())
        raw = b""
        while chunk := s.recv(65536):
            raw += chunk
    finally:
        s.close()
    return int(raw.split(b" ", 2)[1]) if raw else 0


class _Live:
    def __init__(self, app: Any) -> None:
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error",
                                                    lifespan="off", access_log=False))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> int:
        self.thread.start()
        deadline = time.monotonic() + 20
        while not self.server.started:
            assert time.monotonic() < deadline, "uvicorn이 뜨지 않았다"
            time.sleep(0.02)
        return int(self.server.servers[0].sockets[0].getsockname()[1])

    def __exit__(self, *_exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(10)


@pytest.fixture()
def small_pool_app(ctx: Ctx, db_url: str) -> Iterator[Any]:
    """설정으로 연결 풀을 2개(초과 없음)·대기 5초로 줄인 앱. ctx가 시드한 DB를 같이 쓴다."""
    from app.adapters.notifier import LogNotifier
    from app.container import build_container
    from app.main import create_app

    settings = conftest.test_settings(db_url, db_pool_size=2, db_max_overflow=0, db_pool_timeout_s=5)
    container = build_container(settings, storage=FakeStorage(), identity=StaticIdentity(), notifier=LogNotifier())
    yield create_app(container)
    container.engine.dispose()


def test_burst_beyond_thread_limit_does_not_exhaust_pool(ctx: Ctx, small_pool_app: Any) -> None:
    n = 150  # 스레드 한도(40)보다 훨씬 많다
    headers = ctx.as_user(102)
    with _Live(small_pool_app) as port, ThreadPoolExecutor(max_workers=n) as pool:
        t0 = time.monotonic()
        codes = Counter(pool.map(lambda _: _get(port, "/v1/me", headers), range(n)))
        elapsed = time.monotonic() - t0
    assert codes == Counter({200: n}), f"{dict(codes)} ({elapsed:.1f}s) — 연결 풀과 스레드 풀이 서로를 기다렸다"
    assert elapsed < 30
