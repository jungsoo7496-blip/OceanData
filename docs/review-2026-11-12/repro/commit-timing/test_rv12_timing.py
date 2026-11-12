"""rv12 (3): prove with a real uvicorn + raw socket that response bytes are not sent before COMMIT returns."""
from __future__ import annotations

import json
import time

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.seed import SES_A
from rv12_live import LiveServer, raw_request

pytestmark = pytest.mark.integration
MEMBER_A, ADMIN_A = 102, 101
EVENT = {"schema_version": "1.0.0", "session_id": SES_A, "event_code": "VESSEL", "started_at": "2026-11-02T00:00:00Z",
         "timestamp_quality": {"source": "DEVICE", "verified": True}, "source": "MANUAL"}


def runtime_dependants(app):
    from fastapi.routing import iter_route_contexts
    out = []
    for rc in iter_route_contexts(app.router.routes):
        d = getattr(rc, "dependant", None)
        if d is not None:
            out.append((rc.path, rc.methods, d))
    return out


def flip_to_request_scope(app) -> int:
    n = 0

    def walk(d):
        nonlocal n
        for sub in d.dependencies:
            if sub.call is get_db and sub.scope == "function":
                sub.scope = None
                n += 1
            walk(sub)

    for _, _, d in runtime_dependants(app):
        walk(d)
    return n


class Slow:
    def __init__(self, monkeypatch, delay):
        self.state = {"armed": False, "start": None, "end": None}
        real = Session.commit
        st, d = self.state, delay

        def commit(self_):
            if st["armed"]:
                st["armed"] = False
                st["start"] = time.monotonic()
                time.sleep(d)
                r = real(self_)
                st["end"] = time.monotonic()
                return r
            return real(self_)

        monkeypatch.setattr(Session, "commit", commit)

    def arm(self):
        self.state.update(armed=True, start=None, end=None, t_app_start=None, t_app_return=None)


class Probe:
    def __init__(self, app, st):
        self.app, self.st = app, st

    async def __call__(self, scope, receive, send):
        async def snd(m):
            if m["type"] == "http.response.start" and self.st.get("t_app_start") is None:
                self.st["t_app_start"] = time.monotonic()
            await send(m)
        try:
            await self.app(scope, receive, snd)
        finally:
            self.st["t_app_return"] = time.monotonic()


def test_first_byte_after_commit(ctx, monkeypatch):
    delay = 1.2
    slow = Slow(monkeypatch, delay)
    srv = LiveServer(Probe(ctx.client.app, slow.state)).start()
    admin = ctx.as_user(ADMIN_A, mfa=True)
    try:
        def mk_hook():
            r = ctx.client.post("/v1/webhooks/subscriptions", json={"url": "https://example.com/hook",
                                "event_types": ["asset.processed"]}, headers=admin)
            assert r.status_code == 201, r.text
            return r.json()["webhook_id"]

        cases = [
            ("GET dict /v1/me", lambda: ("GET", "/v1/me", None, ctx.as_user(MEMBER_A))),
            ("POST JSONResponse /v1/events", lambda: ("POST", "/v1/events", EVENT, ctx.as_user(MEMBER_A))),
            ("DELETE 204 Response webhook", lambda: ("DELETE", f"/v1/webhooks/subscriptions/{mk_hook()}", None, admin)),
        ]
        for mode in ("function(new)", "request(old control)"):
            if mode.startswith("request"):
                print("flipped dependants:", flip_to_request_scope(ctx.client.app))
            for label, build in cases:
                method, path, body, hdrs = build()
                slow.arm()
                payload = json.dumps(body).encode() if body is not None else b""
                h = dict(hdrs)
                if body is not None:
                    h["Content-Type"] = "application/json"
                res = raw_request(srv.port, method, path, h, payload)
                st = slow.state
                t0 = time.monotonic()
                while st["end"] is None and time.monotonic() - t0 < 5:
                    time.sleep(0.02)
                assert st["end"] is not None, (label, mode, res["status"], res["body"][:200])
                print(f"RESULT {label:30s} {mode:22s} status={res['status']} first_byte_minus_commit_end="
                      f"{(res['t_first'] - st['end']) * 1000:8.1f} ms  commit_took={(st['end'] - st['start']) * 1000:.0f} ms"
                      f"  total={(res['t_end'] - res['t_sent']) * 1000:.0f} ms"
                      f"  app_send_start_minus_commit_end={(st['t_app_start'] - st['end']) * 1000:8.1f} ms"
                      f"  app_return_minus_commit_end={(st['t_app_return'] - st['end']) * 1000:8.1f} ms")
    finally:
        srv.stop()
