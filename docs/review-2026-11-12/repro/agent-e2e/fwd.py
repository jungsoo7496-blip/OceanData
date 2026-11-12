#!/usr/bin/env python3
"""Observation-only TCP forwarder for the e2e12 run.

Bytes are forwarded UNCHANGED in both directions (Host header, Content-Length, signatures stay exactly as the
client sent them), so the real server / real object store see precisely what the real Go agent sent.
Only request lines + status lines are parsed (best effort) to write a one-line-per-request log:

    <utc-time> <label> <METHOD> <path-without-query[+partNumber]> req_cl=<n> -> <status>

Query strings are never logged (presigned signatures, tokens).  usage: fwd.py LISTEN_PORT TARGET_PORT LOGFILE LABEL
"""
import asyncio
import re
import sys
from collections import deque
from datetime import datetime, timezone

LISTEN, TARGET, LOGFILE, LABEL = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4]


def log(line: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
    with open(LOGFILE, "a", encoding="utf-8") as f:
        f.write(f"{ts} {LABEL} {line}\n")


def parse_headers(head: str):
    lines = head.split("\r\n")
    first = lines[0]
    hdrs = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            hdrs[k.strip().lower()] = v.strip()
    return first, hdrs


class Direction:
    """Tracks message boundaries in one direction of an HTTP/1.1 connection."""

    def __init__(self, is_request: bool, pending: deque):
        self.is_request, self.pending = is_request, pending
        self.buf = b""
        self.state = "headers"
        self.remaining = 0

    def feed(self, data: bytes) -> None:
        self.buf += data
        while True:
            if self.state == "headers":
                idx = self.buf.find(b"\r\n\r\n")
                if idx < 0:
                    if len(self.buf) > 65536:
                        self.buf = b""
                        self.state = "ignore"
                    return
                head, self.buf = self.buf[:idx].decode("latin-1"), self.buf[idx + 4:]
                first, hdrs = parse_headers(head)
                cl = int(hdrs["content-length"]) if hdrs.get("content-length", "").isdigit() else 0
                chunked = "chunked" in hdrs.get("transfer-encoding", "").lower()
                if self.is_request:
                    method, target = first.split(" ")[0], first.split(" ")[1] if " " in first else "?"
                    path, _, query = target.partition("?")
                    m = re.search(r"(?:^|&)partNumber=(\d+)", query)
                    if m:
                        path += f"?partNumber={m.group(1)}"
                    self.pending.append((method, path, cl))
                    self.state = "chunked" if chunked else ("body" if cl else "headers")
                    self.remaining = cl
                else:
                    status = int(first.split(" ")[1]) if first.startswith("HTTP/") else 0
                    if 100 <= status < 200:  # interim response, final one follows
                        continue
                    method, path, req_cl = self.pending.popleft() if self.pending else ("?", "?", 0)
                    log(f"{method} {path} req_cl={req_cl} -> {status}"
                        + (f" resp_cl={cl}" if cl else ""))
                    if method == "HEAD" or status in (204, 304):
                        self.state = "headers"
                    elif chunked:
                        self.state = "chunked"
                    elif "content-length" in hdrs:
                        self.state, self.remaining = ("body" if cl else "headers"), cl
                    else:
                        self.state = "ignore"
            elif self.state == "body":
                take = min(len(self.buf), self.remaining)
                self.buf, self.remaining = self.buf[take:], self.remaining - take
                if self.remaining:
                    return
                self.state = "headers"
            elif self.state == "chunked":
                end = self.buf.find(b"\r\n0\r\n\r\n")
                if end < 0:
                    self.buf = self.buf[-8:]
                    return
                self.buf = self.buf[end + 7:]
                self.state = "headers"
            else:  # ignore
                self.buf = b""
                return


async def pipe(reader, writer, direction: Direction):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            try:
                direction.feed(data)
            except Exception as exc:  # parsing must never break forwarding
                log(f"PARSE-ERROR {exc!r}")
                direction.state, direction.buf = "ignore", b""
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def handle(creader, cwriter):
    try:
        sreader, swriter = await asyncio.open_connection("127.0.0.1", TARGET)
    except OSError as exc:
        log(f"CONNECT-FAILED {exc!r}")
        cwriter.close()
        return
    pending: deque = deque()
    await asyncio.gather(pipe(creader, swriter, Direction(True, pending)),
                         pipe(sreader, cwriter, Direction(False, pending)))


async def main():
    server = await asyncio.start_server(handle, "127.0.0.1", LISTEN)
    log(f"listening {LISTEN} -> {TARGET}")
    async with server:
        await server.serve_forever()


asyncio.run(main())
