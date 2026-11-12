"""E6/E7/E12: subscription cap under concurrency, API edge inputs, cross-org delete variants."""
import threading, json
import pytest
from sqlalchemy import select, text, func

from app.db.models import WebhookSubscription
from app.errors import ProblemError
from app.seed import ORG_A, ORG_B
from app.services import webhooks

pytestmark = pytest.mark.integration
ADMIN_A, MEMBER_A, ADMIN_B, OP = 101, 102, 103, 106
PUBLIC_URL = "https://1.1.1.1/marine-hook"


def test_cap_holds_under_concurrent_subscribe(ctx):
    c = ctx.c
    ok, conflict, other = [], [], []
    barrier = threading.Barrier(24)

    def go(i):
        barrier.wait()
        try:
            with c.sessionmaker.begin() as db:
                s, _ = webhooks.subscribe(db, c.settings, organization_id=ORG_A, url=f"https://1.1.1.1/{i}",
                                          event_types=["incident.opened"], by="t", now=c.clock.now())
                ok.append(s.webhook_id)
        except ProblemError as e:
            conflict.append(e)
        except Exception as e:
            other.append(repr(e))

    ths = [threading.Thread(target=go, args=(i,)) for i in range(24)]
    for t in ths: t.start()
    for t in ths: t.join(60)
    with c.sessionmaker() as db:
        held = db.execute(select(func.count()).select_from(WebhookSubscription).where(WebhookSubscription.organization_id == ORG_A)).scalar_one()
    print(f"\ncap={c.settings.webhook_max_subscriptions_per_org} ok={len(ok)} conflict={len(conflict)} other={other} held={held}")
    assert len(ok) == c.settings.webhook_max_subscriptions_per_org and held == len(ok) and not other


@pytest.mark.parametrize("url", [
    "https://1.1.1.1/a\u0000b", "https://1.1.1.1/a\nb", "https://1.1.1.1/a b", "https://1.1.1.1/‮x", "https://1.1.1.1/%00",
    "https://1.1.1.1/" + "a" * 470, "https://1.1.1.1/\U0001F600", "https://exa\u0000mple.com/x",
])
def test_odd_urls_do_not_500(ctx, url):
    r = ctx.client.post("/v1/webhooks/subscriptions", json={"url": url, "event_types": ["asset.processed"]},
                        headers=ctx.as_user(ADMIN_A))
    print(f"\nurl={url[:30]!r}... -> {r.status_code} {r.text[:160]}")
    assert r.status_code in (201, 400, 422), r.text


def test_cross_org_delete_variants(ctx):
    path = "/v1/webhooks/subscriptions"
    wid = ctx.client.post(path, headers=ctx.as_user(ADMIN_A), json={"url": PUBLIC_URL, "event_types": ["asset.processed"]}).json()["webhook_id"]
    # member without webhook:manage, operator, other org admin
    for who in (MEMBER_A, ADMIN_B, OP):
        r = ctx.client.delete(f"{path}/{wid}", headers=ctx.as_user(who))
        print(f"\ndelete by user {who}: {r.status_code} {r.json().get('code')}")
    items = ctx.client.get(path, headers=ctx.as_user(ADMIN_A)).json()["items"]
    assert [x["webhook_id"] for x in items] == [wid]
    with ctx.c.engine.connect() as conn:
        rows = conn.execute(text("SELECT actor_organization_id, details FROM audit_log WHERE action='CROSS_ORG_ACCESS_ATTEMPT'")).all()
    print("audit rows:", [tuple(r) for r in rows])
