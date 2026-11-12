"""E2E wire check with a real TLS receiver + poison-event disable semantics."""
import json, logging, threading, time
from datetime import timedelta
import pytest
from sqlalchemy import text

from app.db.models import WebhookDelivery, WebhookSubscription
from app.seed import ORG_A
from app.services import webhooks
from tests.integration.test_webhooks import _deliveries, _with
from tests.unit import test_webhook_send as T

pytestmark = pytest.mark.integration
ADMIN_A = 101


def test_wire_request_is_signed_and_pinned(ctx, tmp_path, monkeypatch):
    server_ctx, trusted = T.self_signed(tmp_path, "hooks.example.test")
    monkeypatch.setattr(webhooks, "_tls_context", lambda: trusted)
    monkeypatch.setattr(webhooks, "_lookup", lambda host, port: ["127.0.0.1"])
    ctx.c.settings = ctx.c.settings.model_copy(update={"webhook_allow_private_targets": True, "webhook_timeout_s": 3.0})
    rx = T.SyntheticReceiver(T.respond_204, tls=server_ctx)
    url = f"https://hooks.example.test:{rx.port}/services/T1/B2/PATHTOKEN?k=QUERYTOKEN"
    r = ctx.client.post("/v1/webhooks/subscriptions", headers=ctx.as_user(ADMIN_A), json={"url": url, "event_types": ["incident.opened"]})
    assert r.status_code == 201, r.text
    secret, wid = r.json()["secret"], r.json()["webhook_id"]
    with ctx.c.sessionmaker.begin() as db:
        webhooks.emit(db, ORG_A, "incident.opened", {"incident_id": "inc_x"})
    stats = webhooks.deliver_due(ctx.c)
    assert stats["delivered"] == 1, stats
    raw = rx.seen[0]
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    hdr = {l.split(":", 1)[0].lower(): l.split(":", 1)[1].strip() for l in lines[1:]}
    print("\nREQUEST LINE:", lines[0]); print("HEADERS:", {k: (v if k != "x-signature" else v[:12] + "…") for k, v in hdr.items()})
    ts = int(hdr["x-signature-timestamp"])
    assert hdr["x-signature"] == webhooks.sign(secret, ts, body), "signature does not verify on the wire"
    assert hdr["host"] == f"hooks.example.test:{rx.port}" and hdr["x-event-id"] == json.loads(body)["event_id"]
    assert lines[0] == "POST /services/T1/B2/PATHTOKEN?k=QUERYTOKEN HTTP/1.1"
    assert rx.snis == ["hooks.example.test"]
    rx.close()


def test_poison_event_disables_healthy_subscription(ctx, monkeypatch):
    c = _with(ctx, webhook_max_attempts=3)
    with c.sessionmaker.begin() as db:
        s, _ = webhooks.subscribe(db, c.settings, organization_id=ORG_A, url="https://1.1.1.1/h", event_types=["incident.opened"], by="t", now=c.clock.now())
        wid = s.webhook_id
    seen = []
    calls = {"poison": 0}
    def send(url, body, headers, timeout):
        d = json.loads(body)["data"]["incident_id"]
        seen.append(d)
        if d == "POISON":
            calls["poison"] += 1
            if calls["poison"] == 3:
                emit("OK-4-arrives-while-poison-final-attempt-in-flight")
            return 400
        return 204
    def emit(name):
        with c.sessionmaker.begin() as db:
            webhooks.emit(db, ORG_A, "incident.opened", {"incident_id": name})
    t0 = c.clock.now()
    emit("POISON")
    webhooks.deliver_due(c, now=t0, send=send)                         # attempt 1 -> 400, backoff 30s
    emit("OK-1"); emit("OK-2")
    webhooks.deliver_due(c, now=t0 + timedelta(seconds=10), send=send)  # healthy events flow
    webhooks.deliver_due(c, now=t0 + timedelta(seconds=40), send=send)  # attempt 2 of POISON -> 400
    emit("OK-3-late")
    # OK-3-late is PENDING & due, but POISON's 3rd attempt (same round as its claim) disables everything before OK-3 is sent
    webhooks.deliver_due(c, now=t0 + timedelta(seconds=100), send=send)
    rows = {d.payload["data"]["incident_id"]: (d.status, d.attempts) for d in _deliveries(ctx, webhook_id=wid)}
    with c.sessionmaker() as db:
        sub = db.get(WebhookSubscription, wid)
        print("\nstatuses:", rows, "| sub.active:", sub.active, "| consecutive_failures:", sub.consecutive_failures, "| reason:", sub.disabled_reason)
