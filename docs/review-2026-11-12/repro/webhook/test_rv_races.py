"""E3: claim races with several concurrent dispatchers (threads) against one DB."""
import collections, random, threading, time
import pytest
from sqlalchemy import select, func, text

from app.db.models import WebhookDelivery, WebhookSubscription
from app.seed import ORG_A, ORG_B, ORG_CONSUMER
from app.services import webhooks
from tests.integration.test_webhooks import _subscribe, _emit, _with, _deliveries

pytestmark = pytest.mark.integration


def test_many_workers_no_double_send_one_inflight_per_sub(ctx):
    c = _with(ctx, webhook_max_per_org=3, webhook_batch_size=8)
    subs = []
    for org in (ORG_A, ORG_B, ORG_CONSUMER):
        for i in range(5):
            subs.append(_subscribe(c, org, f"https://1.1.1.1/{org[-4:]}-{i}"))
    for org in (ORG_A, ORG_B, ORG_CONSUMER):
        _emit(c, org, 12)
    total = len(_deliveries(ctx))
    lock = threading.Lock()
    inflight = collections.Counter()
    peak = collections.Counter()
    sent = collections.Counter()

    def send(url, body, headers, timeout):
        key = url
        with lock:
            inflight[key] += 1
            peak[key] = max(peak[key], inflight[key])
            sent[(url, headers["X-Event-Id"])] += 1
        time.sleep(random.uniform(0.0, 0.02))
        with lock:
            inflight[key] -= 1
        return 204

    stop = threading.Event()
    errs = []

    def worker():
        try:
            while not stop.is_set():
                s = webhooks.deliver_due(c, send=send, stop=stop)
                if not any(s.values()):
                    time.sleep(0.02)
        except BaseException as e:   # noqa
            errs.append(repr(e))

    ths = [threading.Thread(target=worker) for _ in range(4)]
    for t in ths: t.start()
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        st = collections.Counter(d.status for d in _deliveries(ctx))
        if st.get("DELIVERED") == total:
            break
        time.sleep(0.2)
    stop.set()
    for t in ths: t.join(30)
    st = collections.Counter(d.status for d in _deliveries(ctx))
    dup = {k: v for k, v in sent.items() if v > 1}
    print(f"\ntotal={total} statuses={dict(st)} peak_inflight_per_sub={max(peak.values())} dup_sends={len(dup)} errs={errs}")
    assert not errs
    assert st.get("DELIVERED") == total
    assert max(peak.values()) == 1
    assert not dup
    assert all(d.attempts == 1 for d in _deliveries(ctx))
