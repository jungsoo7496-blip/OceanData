"""E1: does one org's slow receiver slow ANOTHER org's delivery throughput (round barrier)?

ADR-0041 decision 3 claims: "느린 서버는 자기 구독의 처리만 늦춘다".
Org A: subscription to a receiver that accepts TLS, reads the request and never answers (tarpit), M due events.
Org B: subscription to a fast receiver, N due events. Measure how long B's N events take with/without A's tarpit.
"""
import dataclasses, pathlib, socket, tempfile, threading, time
import pytest
from sqlalchemy import select

from app.db.models import WebhookDelivery
from app.seed import ORG_A, ORG_B
from app.services import webhooks
from tests.integration.test_webhooks import _subscribe, _emit, _with, _deliveries
from tests.unit import test_webhook_send as T

pytestmark = pytest.mark.integration
TIMEOUT = 1.0
N = 6


@pytest.fixture()
def tls(tmp_path, monkeypatch):
    server_ctx, trusted = T.self_signed(tmp_path, "hooks.example.test")
    monkeypatch.setattr(webhooks, "_tls_context", lambda: trusted)
    monkeypatch.setattr(webhooks, "_lookup", lambda host, port: ["127.0.0.1"])
    return server_ctx


def _drain_b(ctx, tls, with_tarpit):
    c = _with(ctx, webhook_allow_private_targets=True, webhook_timeout_s=TIMEOUT)
    fast = T.SyntheticReceiver(T.respond_204, tls=tls)
    slow = T.SyntheticReceiver(T.silent, tls=tls)
    try:
        wb = _subscribe(c, ORG_B, f"https://hooks.example.test:{fast.port}/b")
        _emit(c, ORG_B, N)
        if with_tarpit:
            _subscribe(c, ORG_A, f"https://hooks.example.test:{slow.port}/a")
            _emit(c, ORG_A, N + 2)   # attacker keeps due events queued: one due event per round is enough
        t0 = time.monotonic()
        # run dispatcher rounds until all of B's deliveries are DELIVERED
        stop = threading.Event()
        th = threading.Thread(target=lambda: webhooks.deliver_due(c, stop=stop), daemon=True)
        th.start()
        deadline = time.monotonic() + 60
        done_at = None
        while time.monotonic() < deadline:
            st = [d.status for d in _deliveries(ctx, webhook_id=wb)]
            if st and all(s == "DELIVERED" for s in st):
                done_at = time.monotonic() - t0
                break
            time.sleep(0.05)
        stop.set(); th.join(30)
        return done_at
    finally:
        fast.close(); slow.close()


def test_baseline_fast_org_alone(ctx, tls):
    t = _drain_b(ctx, tls, with_tarpit=False)
    print(f"\nBASELINE: org B drains {N} events in {t:.2f}s")
    assert t is not None


def test_fast_org_with_foreign_tarpit(ctx, tls):
    t = _drain_b(ctx, tls, with_tarpit=True)
    print(f"\nWITH FOREIGN TARPIT (timeout={TIMEOUT}s): org B drains {N} events in {t}s")
    assert t is not None
