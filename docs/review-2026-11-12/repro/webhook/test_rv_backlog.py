"""E3b: cost of a claim round with a large due backlog (tarpit sub accumulating deliveries)."""
import time
import pytest
from sqlalchemy import text

from app.seed import ORG_A, ORG_B
from app.services import webhooks
from tests.integration.test_webhooks import _subscribe, _with

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("n", [10_000, 100_000, 400_000])
def test_claim_cost_with_backlog(ctx, n):
    c = _with(ctx, webhook_allow_private_targets=True)
    wa = _subscribe(c, ORG_A, "https://1.1.1.1/a")
    wb = _subscribe(c, ORG_B, "https://1.1.1.1/b")
    with ctx.c.engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO webhook_deliveries (delivery_id, event_id, webhook_id, organization_id, event_type, payload, status, attempts, next_attempt_at, created_at)
            SELECT lpad(to_hex(g), 26, '0'), lpad(to_hex(g), 26, '0'), :w, :o, 'incident.opened', '{"data":{"incident_id":"x"}}'::jsonb, 'PENDING', 0, now() - interval '1 hour' + g * interval '1 millisecond', now()
            FROM generate_series(1, :n) g"""), {"w": wa, "o": ORG_A, "n": n})
        conn.execute(text("""
            INSERT INTO webhook_deliveries (delivery_id, event_id, webhook_id, organization_id, event_type, payload, status, attempts, next_attempt_at, created_at)
            VALUES ('victim00000000000000000001', 'victim00000000000000000001', :w, :o, 'incident.opened', '{"data":{}}'::jsonb, 'PENDING', 0, now(), now())"""), {"w": wb, "o": ORG_B})
        conn.execute(text("ANALYZE webhook_deliveries"))
    now = ctx.c.clock.now()
    t0 = time.monotonic()
    claims, cancelled = webhooks._claim(c, now, 20)
    el = time.monotonic() - t0
    t1 = time.monotonic()
    claims2, _ = webhooks._claim(c, now, 20)
    el2 = time.monotonic() - t1
    print(f"\nbacklog={n}: first _claim {el*1000:.0f} ms (claimed {len(claims)}), second {el2*1000:.0f} ms (claimed {len(claims2)})")
