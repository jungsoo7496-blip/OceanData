"""01번 요구사항 4·04번 요구사항 8.2: 웹훅 URL의 경로에 든 토큰(Slack·Teams 방식)이 로그에 남지 않는다 (지시서 12번 독립 검토 — ADR-0057).

httpx는 요청마다 `HTTP Request: POST <전체 URL> "HTTP/1.1 204"`를 INFO로 남긴다. 로그 정리기는 쿼리만 지우므로 경로의 토큰이
그대로 나갔다. 그 로거는 WARNING 이상만 남긴다. 시험 데이터는 합성 값이다.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from app.logging_setup import configure_logging

SECRET = "PATHSECRET123"


class _Ok(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_a: object) -> None:  # 시험 서버의 접근 로그는 끈다
        return


@pytest.fixture()
def receiver() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _Ok)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/services/T0001/B0002/{SECRET}"
    server.shutdown()
    thread.join(5)


@pytest.fixture()
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    saved = (root.handlers[:], root.level, {n: logging.getLogger(n).level for n in ("httpx", "httpcore")})
    yield
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    for name, level in saved[2].items():
        logging.getLogger(name).setLevel(level)


def test_info_log_does_not_contain_the_url_path_token(receiver: str, restore_logging: None,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    assert httpx.post(receiver, content=b"{}").status_code == 204
    out = capsys.readouterr().out
    assert SECRET not in out, out


def test_debug_level_still_hides_it_from_the_http_libraries(receiver: str, restore_logging: None,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    """LOG_LEVEL=DEBUG(문제 추적용)에서도 HTTP 라이브러리는 요청 줄을 남기지 않는다. 앱 로그는 DEBUG를 그대로 따른다."""
    configure_logging("DEBUG")
    assert httpx.post(receiver, content=b"{}").status_code == 204
    logging.getLogger("app.test").debug("앱 디버그 로그")
    out = capsys.readouterr().out
    assert SECRET not in out, out
    assert "앱 디버그 로그" in out
