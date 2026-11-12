"""rv12: is the thread-pool/DB-pool circular wait under a burst pre-existing? Same test runs on base and on the change."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, text

from rv12_live import LiveServer, raw_request

pytestmark = pytest.mark.integration


def test_burst_of_plain_requests(ctx):
    diag = create_engine(ctx.c.engine.url, pool_size=2, max_overflow=0)
    srv = LiveServer(ctx.client.app).start()
    try:
        hdr = ctx.as_user(102)
        N = 150

        def fire(i):
            return raw_request(srv.port, "GET", "/v1/me", hdr, timeout=90)

        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=N) as pool:
            res = list(pool.map(fire, range(N)))
        dt = time.monotonic() - t0
        codes = {}
        for r in res:
            codes[r["status"]] = codes.get(r["status"], 0) + 1
        slow = sorted((r["t_end"] - r["t_sent"]) for r in res)
        print(f"BURST N={N} total={dt:.1f}s statuses={codes} p50={slow[len(slow)//2]:.2f}s max={slow[-1]:.2f}s pool_after={ctx.c.engine.pool.status()}")
    finally:
        srv.stop()
