"""04번 요구사항 8.2·8.3 / ADR-0041 결정 3·ADR-0070: 한 기관의 느린 수신 서버가 다른 기관의 웹훅 전송을 늦추지 않는다.

검토 2026-11-12 웹훅 W1(지시서 12번 F1)의 재현이다. 예전에는 `deliver_due`가 한 라운드의 전송이 모두 끝나야 다음 라운드를
시작해(`asyncio.run(_send_all(...))` 뒤에 다음 `_claim`), 응답하지 않는 수신 서버의 구독 하나가 라운드마다 제한 시간을 통째로 썼고
다른 기관의 같은 시간대 전송이 그만큼 늦어졌다(기관 B 6건: 0.3초 → 6초 남짓). ADR-0070부터는 전송마다 따로 보내고, 한 건을
기록하면 그 구독의 다음 건을 바로 가져간다. 시험 데이터는 합성 값이다.

벽시계 시간은 기계가 바쁘면 함께 늘어난다. 전체 시험을 다른 작업과 함께 돌린 날 기관 B만 보내는 데도 3초를 넘겼고, 응답하는
수신 서버로의 전송이 1초 기한에 걸려 재시도 간격(30초) 뒤로 밀리기도 했다. 그래서 절대 시간 대신 같은 시험 안에서 잰 기준선
(느린 수신 서버 없음)과 비교하고, 기한은 운영 기본값(5초)으로 넉넉히 두며, 재시도 간격을 1초로 줄여 바쁜 기계에서 기관 B의
재시도 한 번이 결과를 뒤집지 않게 한다. 라운드 장벽이 있으면 기관 B는 라운드(기한)마다 한 건씩 나가 기준선보다
EVENTS × TIMEOUT(30초) 남짓 늦으므로, 허용한 차이(2 × TIMEOUT)와 넉넉히 갈린다.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.db.models import WebhookSubscription
from app.seed import ORG_A, ORG_B
from app.services import webhooks
from tests.integration.test_webhooks import _deliveries, _emit, _subscribe, _with
from tests.unit import test_webhook_send as T

pytestmark = pytest.mark.integration
TIMEOUT = 5.0  # 전송 한 건의 총 기한(초) — 운영 기본값
EVENTS = 6


def _seconds_to_drain_org_b(ctx, tls, *, foreign_tarpit: bool) -> float:  # type: ignore[no-untyped-def]
    c = _with(ctx, webhook_allow_private_targets=True, webhook_timeout_s=TIMEOUT, webhook_backoff_base_s=1)
    fast = T.SyntheticReceiver(T.respond_204, tls=tls)
    slow = T.SyntheticReceiver(T.silent, tls=tls)
    try:
        hook_b = _subscribe(c, ORG_B, f"https://hooks.example.test:{fast.port}/b")
        _emit(c, ORG_B, EVENTS)
        if foreign_tarpit:
            _subscribe(c, ORG_A, f"https://hooks.example.test:{slow.port}/a")
            _emit(c, ORG_A, EVENTS + 2)  # 계속 보낼 것이 남아 있으면 라운드마다 제한 시간을 쓴다
        started = time.monotonic()
        stop = threading.Event()
        worker = threading.Thread(target=lambda: webhooks.deliver_due(c, stop=stop), daemon=True)
        worker.start()
        done = None
        while time.monotonic() - started < 60:
            statuses = [d.status for d in _deliveries(ctx, webhook_id=hook_b)]
            if statuses and all(s == "DELIVERED" for s in statuses):
                done = time.monotonic() - started
                break
            time.sleep(0.05)
        stop.set()
        worker.join(30)
        assert done is not None, "기관 B의 전송이 60초 안에 끝나지 않았다"
        with c.sessionmaker.begin() as db:  # 같은 시험에서 다시 잴 때 이 구독이 기관 B의 새 이벤트를 나눠 받지 않게
            sub = db.get(WebhookSubscription, hook_b)
            assert sub is not None
            sub.active = False
        return done
    finally:
        fast.close()
        slow.close()


@pytest.fixture()
def tls(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    server_ctx, trusted = T.self_signed(tmp_path, "hooks.example.test")
    monkeypatch.setattr(webhooks, "_tls_context", lambda: trusted)
    monkeypatch.setattr(webhooks, "_lookup", lambda host, port: ["127.0.0.1"])
    return server_ctx


def test_baseline_a_fast_organization_alone_drains_quickly(ctx, tls) -> None:  # type: ignore[no-untyped-def]
    assert _seconds_to_drain_org_b(ctx, tls, foreign_tarpit=False) < 3 * TIMEOUT


def test_a_foreign_tarpit_does_not_delay_another_organization(ctx, tls) -> None:  # type: ignore[no-untyped-def]
    # 예전에는 라운드(제한 시간)마다 기관 B가 구독당 1건씩만 나가 6건에 EVENTS × TIMEOUT 남짓 걸렸다. 이제는 같은 시험에서
    # 잰 기준선에 가깝다. 기준선을 바로 앞에서 재므로 기계가 바쁜 정도가 두 값에 함께 들어간다.
    baseline = _seconds_to_drain_org_b(ctx, tls, foreign_tarpit=False)
    with_tarpit = _seconds_to_drain_org_b(ctx, tls, foreign_tarpit=True)
    assert with_tarpit - baseline < 2 * TIMEOUT, f"기준선 {baseline:.1f}초, 느린 수신 서버가 있을 때 {with_tarpit:.1f}초"
