"""rv12 global probe: wraps every app built by app.main.create_app with an outermost ASGI middleware that records,
per HTTP request: number of Session objects created, whether any Session.commit/close/rollback ran after the response
started (old behaviour), whether sessions were left open when the app returned."""
from __future__ import annotations

import collections
import contextvars
import json
import os
import threading
import time

import pytest
from sqlalchemy.orm import Session

req_state: contextvars.ContextVar = contextvars.ContextVar("rv12_req_state", default=None)
LOCK = threading.Lock()
RECORDS: list[dict] = []
SUMMARY = collections.Counter()

_real_init = Session.__init__
_real_commit = Session.commit
_real_close = Session.close
_real_rollback = Session.rollback


def _init(self, *a, **kw):
    st = req_state.get()
    if st is not None:
        st["sessions"] += 1
        self._rv12_state = st
        st["open"] += 1
    return _real_init(self, *a, **kw)


def _commit(self):
    st = getattr(self, "_rv12_state", None)
    if st is not None:
        st["commits"].append(st["started"])
    return _real_commit(self)


def _rollback(self):
    st = getattr(self, "_rv12_state", None)
    if st is not None:
        st["rollbacks"].append(st["started"])
    return _real_rollback(self)


def _close(self):
    st = getattr(self, "_rv12_state", None)
    if st is not None and not getattr(self, "_rv12_closed", False):
        self._rv12_closed = True
        st["open"] -= 1
        st["closes"].append(st["started"])
    return _real_close(self)


Session.__init__ = _init
Session.commit = _commit
Session.rollback = _rollback
Session.close = _close


class ProbeMW:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        st = {"started": False, "sessions": 0, "open": 0, "commits": [], "rollbacks": [], "closes": [], "status": None,
              "method": scope["method"], "path": scope["path"]}
        tok = req_state.set(st)

        async def snd(m):
            if m["type"] == "http.response.start":
                st["started"] = True
                st["status"] = m["status"]
            await send(m)

        try:
            await self.app(scope, receive, snd)
        finally:
            req_state.reset(tok)
            late_commit = any(st["commits"])  # commit that ran after response.start
            late_close = any(st["closes"])
            late_rollback = any(st["rollbacks"])
            rec = {"m": st["method"], "p": st["path"], "status": st["status"], "sessions": st["sessions"],
                   "late_commit": late_commit, "late_close": late_close, "late_rollback": late_rollback,
                   "open_after_return": st["open"]}
            with LOCK:
                SUMMARY["requests"] += 1
                if st["sessions"] > 1:
                    SUMMARY["multi_session"] += 1
                if late_commit:
                    SUMMARY["late_commit"] += 1
                if late_close:
                    SUMMARY["late_close"] += 1
                if late_rollback:
                    SUMMARY["late_rollback"] += 1
                if st["open"] != 0:
                    SUMMARY["open_after_return"] += 1
                if st["sessions"] == 0:
                    SUMMARY["no_session"] += 1
                if st["sessions"] > 1 or late_commit or late_close or late_rollback or st["open"] != 0:
                    RECORDS.append(rec)


def pytest_configure(config):
    import app.main as m

    real = m.create_app

    def create_app(c=None):
        a = real(c)
        a.add_middleware(ProbeMW)
        return a

    m.create_app = create_app


def pytest_sessionfinish(session, exitstatus):
    out = os.environ.get("RV12_PROBE_OUT")
    if out:
        with open(out, "w") as f:
            json.dump({"summary": dict(SUMMARY), "records": RECORDS[:500]}, f, indent=1, ensure_ascii=False)


def pytest_terminal_summary(terminalreporter):
    terminalreporter.write_line(f"RV12 PROBE SUMMARY: {dict(SUMMARY)}")
    for r in RECORDS[:30]:
        terminalreporter.write_line(f"RV12 PROBE RECORD: {r}")
