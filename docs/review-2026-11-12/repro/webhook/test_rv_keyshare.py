"""A business transaction that just enqueued a delivery for the same subscription holds FK KEY SHARE on the subscription row;
_record's FOR UPDATE on that row conflicts with it -> record fails after lock_timeout, delivery stays SENDING -> re-sent (duplicate)."""
import threading, time
from datetime import timedelta
import pytest
from sqlalchemy import text

from app.seed import ORG_A
from app.services import webhooks
from tests.integration.test_webhooks import _subscribe, _emit, _with, _deliveries

pytestmark = pytest.mark.integration


def test_open_business_tx_blocks_record(ctx):
    c = _with(ctx, webhook_timeout_s=1.0)
    wid = _subscribe(c, ORG_A, "https://1.1.1.1/h")
    _emit(c, ORG_A, 1)
    sent = []
    def send(url, body, headers, timeout):
        sent.append(headers["X-Event-Id"]); return 204
    # business transaction: inserts a delivery for the same subscription and stays open (FK check => FOR KEY SHARE on the sub row)
    conn = ctx.c.engine.connect()
    tx = conn.begin()
    conn.execute(text("""INSERT INTO webhook_deliveries (delivery_id, event_id, webhook_id, organization_id, event_type, payload, status, attempts, next_attempt_at, created_at)
        VALUES ('biz00000000000000000000001','biz00000000000000000000001', :w, :o, 'incident.opened', '{}'::jsonb, 'PENDING', 0, now() + interval '1 hour', now())"""), {"w": wid, "o": ORG_A})
    t0 = time.monotonic()
    stats = webhooks.deliver_due(c, send=send)
    took = time.monotonic() - t0
    d = [x for x in _deliveries(ctx, webhook_id=wid) if x.delivery_id != 'biz00000000000000000000001'][0]
    print(f"\ndeliver_due returned after {took:.2f}s stats={stats}; receiver got {len(sent)} request(s); row status={d.status} attempts={d.attempts}")
    tx.rollback(); conn.close()
    # after the lease the SENDING row is sent again (duplicate to the receiver)
    stats2 = webhooks.deliver_due(c, send=send, now=c.clock.now() + timedelta(seconds=300))
    print(f"after business tx ended + lease expiry: stats={stats2}; receiver got {len(sent)} request(s) for the same event_id: {sent}")
