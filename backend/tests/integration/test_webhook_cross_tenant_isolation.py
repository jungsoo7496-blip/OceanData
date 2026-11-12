"""04번 요구사항 8.2·8.3 / ADR-0041 결정 3: 한 기관의 느린 수신 서버가 다른 기관의 웹훅 전송을 늦추지 않는다.

**알려진 미해결 (지시서 12번 독립 검토 웹훅 F1).** 지금은 `deliver_due`가 한 라운드의 전송이 모두 끝나야 다음 라운드를 시작한다
(`asyncio.run(_send_all(...))` 뒤에 다음 `_claim`). 응답하지 않는 수신 서버의 구독 하나가 라운드마다 제한 시간을 통째로 쓰므로,
다른 기관의 같은 시간대 전송이 그만큼 늦어진다 — 기관당·구독당 몫을 나눠도 라운드 장벽은 기관 사이를 막는다.
고치는 방향(장벽 없이 전송마다 독립 작업으로, 또는 실패한 수신 서버를 구독 단위로 쉬게 하기)은 전송 구조와 '독살 이벤트' 의미(F5)에
함께 걸려 ADR로 정한 뒤 고친다. 그때까지 이 시험은 strict xfail로 현상을 고정한다 — 고쳐지면 XPASS로 실패하니 표시를 지운다.
시험 데이터는 합성 값이다.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.seed import ORG_A, ORG_B
from app.services import webhooks
from tests.integration.test_webhooks import _deliveries, _emit, _subscribe, _with
from tests.unit import test_webhook_send as T

pytestmark = pytest.mark.integration
TIMEOUT = 1.0  # 전송 한 건의 총 기한(초)
EVENTS = 6


def _seconds_to_drain_org_b(ctx, tls, *, foreign_tarpit: bool) -> float:  # type: ignore[no-untyped-def]
    c = _with(ctx, webhook_allow_private_targets=True, webhook_timeout_s=TIMEOUT)
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


@pytest.mark.xfail(strict=True, reason="웹훅 F1: 라운드 장벽 때문에 다른 기관의 느린 수신 서버가 전송을 늦춘다 (ADR-0041 결정 3과 어긋남)")
def test_a_foreign_tarpit_does_not_delay_another_organization(ctx, tls) -> None:  # type: ignore[no-untyped-def]
    # 지금은 라운드(제한 시간 1초)마다 기관 B가 구독당 1건씩만 나가 6건에 6초 남짓 걸린다. 고치면 기준선(0.3초)에 가까워야 한다.
    assert _seconds_to_drain_org_b(ctx, tls, foreign_tarpit=True) < 3 * TIMEOUT
