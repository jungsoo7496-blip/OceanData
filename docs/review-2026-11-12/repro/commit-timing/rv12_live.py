"""Scratch helpers (review rv12-commit): real uvicorn in a thread + raw socket client."""
from __future__ import annotations

import socket
import threading
import time
from typing import Any

import uvicorn


class LiveServer:
    def __init__(self, app: Any) -> None:
        cfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off", loop="asyncio",
                             http="h11", access_log=False)
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True, name="uvicorn-rv12")
        self.port = 0

    def start(self) -> "LiveServer":
        self.thread.start()
        t0 = time.monotonic()
        while not self.server.started:
            if time.monotonic() - t0 > 20:
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.02)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def raw_request(port: int, method: str, path: str, headers: dict[str, str] | None = None, body: bytes = b"",
                timeout: float = 30.0, close_after_send: bool = False, abort_after: float | None = None):
    """Returns dict(t_sent, t_first, t_end, raw, status, headers_raw, body)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    h = {"Host": "x", "Connection": "close", **(headers or {})}
    if body:
        h["Content-Length"] = str(len(body))
    req = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n"
    s.sendall(req.encode() + body)
    t_sent = time.monotonic()
    if abort_after is not None:
        time.sleep(abort_after)
        s.close()
        return {"t_sent": t_sent, "aborted": True}
    t_first = None
    raw = b""
    while True:
        chunk = s.recv(65536)
        if chunk and t_first is None:
            t_first = time.monotonic()
        if not chunk:
            break
        raw += chunk
    t_end = time.monotonic()
    s.close()
    head, _, rest = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1]) if head else 0
    return {"t_sent": t_sent, "t_first": t_first, "t_end": t_end, "raw": raw, "status": status,
            "head": head.decode("latin-1"), "body": rest}
