"""04번 요구사항 8.2·8.3 / ADR-0070: 라운드 장벽 없는 웹훅 전송.

전송마다 따로 보내고, 한 건을 기록하면 그 자리와 그 구독의 다음 건을 바로 가져간다. 그러면서 ADR-0041의 약속을 지킨다:
구독당 한 번에 한 건, 기관당 보내는 중인 건은 WEBHOOK_MAX_PER_ORG건까지, 모두 WEBHOOK_BATCH_SIZE건까지, 워커 여럿이 같은 건을
겹쳐 보내지 않음, 멈추면 보내던 건을 기록하고 끝남, 임대는 (실제로 가져온 시각부터) 보내기 + 기록 차례보다 길다. 가져올 건수는
DB 스레드에서 돌 때 정해 줄 선 기록 뒤의 빈자리를 한꺼번에 채운다. 가져오기가 실패하면 보내던 건을 기록한 뒤 올린다. 비활성 알림은 커밋 뒤,
다른 전송을 막지 않고, 실패해도 로그만 남긴다. 대상은 공인 IP 리터럴이고 전송 함수를 바꿔 실제로 보내지 않는다. 시험 데이터는 합성 값이다.
"""

from __future__ import annotations

import collections
import dataclasses
import logging
import random
import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.metrics import WORKER_REGISTRY
from app.ports.notifier import Notification
from app.seed import ORG_A, ORG_B, ORG_CONSUMER
from app.services import webhooks
from tests.integration.test_webhooks import PUBLIC_URL, Receiver, _deliveries, _emit, _subscribe, _with

pytestmark = pytest.mark.integration


def _sending_by_org(ctx) -> dict[str, int]:  # type: ignore[no-untyped-def]
    with ctx.c.engine.connect() as conn:
        rows = conn.execute(text("SELECT organization_id, count(*) FROM webhook_deliveries "
                                 "WHERE status = 'SENDING' GROUP BY organization_id")).all()
    return {org: int(n) for org, n in rows}


def _delivered(ctx, webhook_id: str) -> int:  # type: ignore[no-untyped-def]
    return sum(d.status == "DELIVERED" for d in _deliveries(ctx, webhook_id=webhook_id))


def test_dead_subscriptions_hold_only_their_org_share_while_another_org_flows(ctx, monkeypatch) -> None:
    """기관 A의 응답 없는 구독 다섯 개가 빈자리를 차례로 노려도 보내는 중인 건은 기관당 WEBHOOK_MAX_PER_ORG건까지라
    기관 B의 전송은 기다리지 않고 흐른다. 라운드가 없으면 가져올 때마다의 몫만으로는 A가 모든 자리를 차지한다."""
    timeout = 0.5
    c = _with(ctx, webhook_max_per_org=2, webhook_batch_size=3, webhook_timeout_s=timeout)
    for i in range(5):
        _subscribe(c, ORG_A, f"https://1.1.1.1/dead{i}")
    _emit(c, ORG_A, 3)  # 기관 A가 더 오래 기다렸다 (구독마다 3건)
    hook_b = _subscribe(c, ORG_B, "https://1.1.1.1/live")
    _emit(c, ORG_B, 6)
    release = threading.Event()

    def send(url: str, body: bytes, headers: dict[str, str], timeout_s: float) -> int:
        if "/dead" in url:
            release.wait(10)  # 기한(0.5초)에 끊긴다. 버려진 스레드는 시험 끝에 놓아 준다
        return 204

    peak: collections.Counter[str] = collections.Counter()
    original = webhooks._claim

    def watched(*args, **kwargs):  # type: ignore[no-untyped-def]
        out = original(*args, **kwargs)
        held = _sending_by_org(ctx)  # 보내는 중인 건은 가져올 때만 는다 — 가져온 직후가 가장 많다
        for org, n in held.items():
            peak[org] = max(peak[org], n)
        peak["total"] = max(peak["total"], sum(held.values()))
        return out

    monkeypatch.setattr(webhooks, "_claim", watched)
    stop = threading.Event()
    worker = threading.Thread(target=lambda: webhooks.deliver_due(c, send=send, stop=stop), daemon=True)
    started = time.monotonic()
    worker.start()
    drained = None
    try:
        while time.monotonic() - started < 20:
            if _delivered(ctx, hook_b) == 6:
                drained = time.monotonic() - started
                break
            time.sleep(0.02)
    finally:
        stop.set()
        worker.join(10)
        release.set()
    assert not worker.is_alive()
    # 고치기 전: 기관 B는 라운드(기한 0.5초)마다 한 건 — 6건에 2.5초 넘게 걸렸다
    assert drained is not None and drained < 2 * timeout, drained
    assert peak[ORG_A] == 2 and peak["total"] <= 3, dict(peak)


def test_disable_notice_is_sent_after_commit_without_stalling_other_deliveries(ctx) -> None:
    """구독을 끈 알림은 그 기록을 커밋한 뒤 보내고, 메일 서버가 느려도 다른 구독의 전송은 그동안 계속 나간다
    (예전에는 라운드 끝의 알림이 다음 라운드를 막았다)."""
    bad = _subscribe(ctx.c, ORG_A, "https://1.1.1.1/bad")
    _emit(ctx.c, ORG_A)
    good = _subscribe(ctx.c, ORG_B, "https://1.1.1.1/good")
    _emit(ctx.c, ORG_B, 5)
    seen: dict[str, object] = {}

    class SlowMail:
        def __init__(self) -> None:
            self.sent: list[Notification] = []

        def send(self, note: Notification) -> None:
            with ctx.c.engine.connect() as conn:
                seen["active"] = conn.execute(text("SELECT active FROM webhook_subscriptions WHERE webhook_id = :w"),
                                              {"w": bad}).scalar()
            time.sleep(1.0)  # 느린 메일 서버
            seen["good_delivered"] = _delivered(ctx, good)
            self.sent.append(note)

    mail = SlowMail()
    c = dataclasses.replace(_with(ctx, webhook_max_attempts=1), notifier=mail)

    def send(url: str, body: bytes, headers: dict[str, str], timeout_s: float) -> int:
        return 500 if url.endswith("/bad") else 204

    stats = webhooks.deliver_due(c, send=send)
    assert stats == {"delivered": 5, "retry": 0, "failed": 1, "cancelled": 0}
    assert [n.kind for n in mail.sent] == ["webhook.disabled"]  # 돌아오기 전에 알림까지 보냈다
    assert seen["active"] is False  # 끈 것을 커밋한 뒤에 보냈다
    assert seen["good_delivered"] == 5  # 메일을 보내는 1초 동안 다른 기관의 전송이 모두 나갔다


def test_stop_records_in_flight_and_claims_nothing_new(ctx) -> None:
    """stop: 새로 가져오지 않는다. 보내던 건은 끝까지(기한 안) 보내 기록하고 바로 끝난다."""
    timeout = 1.5
    c = _with(ctx, webhook_timeout_s=timeout)
    gate = _subscribe(c, ORG_A, "https://1.1.1.1/gate")
    tarpit = _subscribe(c, ORG_B, "https://1.1.1.1/tarpit")
    _emit(c, ORG_A, 3)
    _emit(c, ORG_B, 3)
    lock, both, release, never = threading.Lock(), threading.Event(), threading.Event(), threading.Event()
    started: list[str] = []

    def send(url: str, body: bytes, headers: dict[str, str], timeout_s: float) -> int:
        with lock:
            started.append(url)
            if len(started) == 2:
                both.set()
        (release if url.endswith("/gate") else never).wait(10)  # tarpit은 기한에 끊긴다
        return 204

    stop = threading.Event()
    out: dict[str, dict[str, int]] = {}
    worker = threading.Thread(target=lambda: out.setdefault("stats", webhooks.deliver_due(c, send=send, stop=stop)))
    worker.start()
    try:
        assert both.wait(5)  # 두 구독의 첫 건을 보내는 중
        stop.set()
        stopped = time.monotonic()
        release.set()
        worker.join(10)
        assert not worker.is_alive() and time.monotonic() - stopped < timeout + 1.0  # 보내던 건(기한 안)만 기다렸다
    finally:
        stop.set()
        release.set()
        never.set()
    assert out["stats"] == {"delivered": 1, "retry": 1, "failed": 0, "cancelled": 0}
    assert sorted(started) == sorted(["https://1.1.1.1/gate", "https://1.1.1.1/tarpit"])  # 멈춘 뒤 새로 보내지 않았다
    gate_rows = [(d.status, d.attempts) for d in _deliveries(ctx, webhook_id=gate)]
    assert sorted(gate_rows) == [("DELIVERED", 1), ("PENDING", 0), ("PENDING", 0)]
    tarpit_rows = [(d.status, d.attempts, d.last_error) for d in _deliveries(ctx, webhook_id=tarpit)]
    assert sorted(tarpit_rows, key=str) == sorted([("PENDING", 1, "TimeoutError"), ("PENDING", 0, None),
                                                   ("PENDING", 0, None)], key=str)
    assert _sending_by_org(ctx) == {}  # 임대를 남기지 않았다


@pytest.mark.parametrize("workers", [2, 4])
def test_concurrent_dispatchers_send_each_delivery_once(ctx, workers: int) -> None:
    """워커(디스패처) 여럿이 동시에 돌아도 같은 건을 두 번 보내지 않고, 구독 하나에는 한 번에 한 건만 보낸다."""
    c = _with(ctx, webhook_max_per_org=3, webhook_batch_size=6)
    for org in (ORG_A, ORG_B, ORG_CONSUMER):
        for i in range(4):
            _subscribe(c, org, f"https://1.1.1.1/{org}-{i}")
        _emit(c, org, 6)
    total = len(_deliveries(ctx))
    assert total == 3 * 4 * 6
    lock = threading.Lock()
    rng = random.Random(20261112)
    in_flight: collections.Counter[str] = collections.Counter()
    peak: collections.Counter[str] = collections.Counter()
    sent: collections.Counter[tuple[str, str]] = collections.Counter()

    def send(url: str, body: bytes, headers: dict[str, str], timeout_s: float) -> int:
        with lock:
            in_flight[url] += 1
            peak[url] = max(peak[url], in_flight[url])
            sent[(url, headers["X-Event-Id"])] += 1
            pause = rng.uniform(0.0, 0.02)
        time.sleep(pause)
        with lock:
            in_flight[url] -= 1
        return 204

    stop = threading.Event()
    errors: list[str] = []

    def worker() -> None:
        try:
            while not stop.is_set():
                if not any(webhooks.deliver_due(c, send=send, stop=stop).values()):
                    stop.wait(0.02)
        except BaseException as exc:  # noqa: BLE001 - 시험이 보고한다
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    try:
        until = time.monotonic() + 60
        while time.monotonic() < until and sum(d.status == "DELIVERED" for d in _deliveries(ctx)) < total:
            time.sleep(0.1)
    finally:
        stop.set()
        for t in threads:
            t.join(30)
    rows = _deliveries(ctx)
    assert errors == []
    assert collections.Counter(d.status for d in rows) == {"DELIVERED": total}
    assert len(sent) == total and set(sent.values()) == {1}  # 겹쳐 보낸 건이 없다
    assert set(peak.values()) == {1}  # 구독당 한 번에 한 건
    assert {d.attempts for d in rows} == {1}


def test_lease_outlives_a_full_length_send_and_a_slow_record(ctx, monkeypatch) -> None:
    """임대는 기한 가까이 걸린 보내기와 늦은 기록(기록 차례·잠금 대기)을 합한 것보다 길다. 그동안 다른 워커가
    같은 건을 다시 가져가지 않고, 결과는 처음 임대로 기록된다."""
    timeout = 0.4
    c = _with(ctx, webhook_timeout_s=timeout)
    wid = _subscribe(c, ORG_A, PUBLIC_URL)
    _emit(c, ORG_A)
    original = webhooks._record

    def slow_record(*args, **kwargs):  # type: ignore[no-untyped-def]
        time.sleep(timeout)  # 기록이 기한(잠금 대기 한도)만큼 늦다
        return original(*args, **kwargs)

    monkeypatch.setattr(webhooks, "_record", slow_record)
    calls: list[str] = []
    sending = threading.Event()

    def send(url: str, body: bytes, headers: dict[str, str], timeout_s: float) -> int:
        calls.append(headers["X-Event-Id"])
        sending.set()
        time.sleep(timeout * 0.75)  # 기한 안이지만 거의 다 쓴다
        return 204

    out: dict[str, dict[str, int]] = {}
    first = threading.Thread(target=lambda: out.setdefault("first", webhooks.deliver_due(c, limit=1, send=send)))
    first.start()
    assert sending.wait(5)  # 첫 워커가 가져가 보내는 중
    others: list[dict[str, int]] = []
    while first.is_alive():  # 다른 워커가 보내기·기록 내내 가져가 본다
        others.append(webhooks.deliver_due(c, send=send))
        time.sleep(0.05)
    first.join()
    assert out["first"] == {"delivered": 1, "retry": 0, "failed": 0, "cancelled": 0}
    assert len(others) >= 3 and all(not any(s.values()) for s in others)
    d = _deliveries(ctx, webhook_id=wid)[0]
    assert calls == [d.event_id] and (d.status, d.attempts) == ("DELIVERED", 1)


def test_lease_counts_from_the_real_claim_when_records_queue_in_the_db_thread(ctx, monkeypatch) -> None:
    """임대는 DB 스레드에서 실제로 가져온 시각부터 잰다. 동시 건수 상한 4에서 기록마다 기한만큼 늦어 가져오기가
    기록 셋 뒤에 줄 서고, 그렇게 늦게 가져온 건이 기한 가까이 보내는 동안 다른 건의 기록 셋이 그 앞에 서도, 그 건의
    기록은 임대 안에 끝나 다른 워커가 다시 보내지 않는다. 가져오기를 넣을 때 시각을 읽으면 줄 선 만큼(기한 × 3)
    임대가 짧아져 기록 전에 끝나고, 다른 워커가 같은 건을 두 번째로 보냈다 (검토에서 찾음)."""
    timeout = 0.5
    c = _with(ctx, webhook_timeout_s=timeout, webhook_max_per_org=10, webhook_batch_size=4)
    hooks = [_subscribe(c, ORG_A, f"https://1.1.1.1/s{i}") for i in range(8)]
    _emit(c, ORG_A)  # 구독마다 한 건
    base = c.clock.now() - timedelta(seconds=60)
    with ctx.c.engine.begin() as conn:  # 기한을 구독 순서대로: s0~s3을 먼저, s4~s7을 그다음에 가져간다
        for i, wid in enumerate(hooks):
            conn.execute(text("UPDATE webhook_deliveries SET next_attempt_at = :t WHERE webhook_id = :w"),
                         {"t": base + timedelta(seconds=i), "w": wid})
    original = webhooks._record

    def slow_record(*args, **kwargs):  # type: ignore[no-untyped-def]
        time.sleep(timeout)  # 기록마다 잠금 대기 한도(기한)만큼 늦다
        return original(*args, **kwargs)

    monkeypatch.setattr(webhooks, "_record", slow_record)
    lock, all_sent = threading.Lock(), threading.Event()
    sent: collections.Counter[str] = collections.Counter()

    def send(url: str, body: bytes, headers: dict[str, str], timeout_s: float) -> int:
        with lock:
            sent[url.rsplit("/", 1)[1]] += 1
            if len(sent) == len(hooks):
                all_sent.set()
        if url.endswith("/s4"):
            time.sleep(timeout * 0.9)  # 늦게 가져온 건: 기한 안이지만 거의 다 쓴다 — 그동안 s5~s7의 기록이 앞에 선다
        return 204

    out: dict[str, dict[str, int]] = {}
    first = threading.Thread(target=lambda: out.setdefault("first", webhooks.deliver_due(c, send=send)))
    first.start()
    assert all_sent.wait(10)  # 여덟 건을 모두 가져가 보냈다 (s4는 아직 보내는 중, s5~s7은 기록 차례)
    others: list[dict[str, int]] = []
    while first.is_alive():  # 다른 워커가 남은 기록이 끝날 때까지 가져가 본다
        others.append(webhooks.deliver_due(c, limit=1, send=send))
        time.sleep(0.05)
    first.join()
    assert set(sent.values()) == {1}, {k: n for k, n in sent.items() if n > 1}  # 고치기 전: s4를 두 번 보냈다
    assert out["first"] == {"delivered": len(hooks), "retry": 0, "failed": 0, "cancelled": 0}
    assert len(others) >= 3 and all(not any(s.values()) for s in others)
    assert {(d.status, d.attempts) for d in _deliveries(ctx)} == {("DELIVERED", 1)}


def test_claim_takes_every_slot_freed_while_it_waited_for_the_db_thread(ctx, monkeypatch) -> None:
    """가져올 건수는 DB 스레드에서 실제로 가져올 때 정한다. 한 건의 기록이 끝나 넣은 가져오기는 이미 줄 선 다른 기록
    셋 뒤에 도는데, 그때는 네 자리가 모두 비어 있으니 네 건을 가져간다. 그 가져오기가 이미 본 기록으로는 다시 가져오지
    않아, 가져오기 횟수는 라운드 때와 같다(네 건씩 세 번 + 끝을 확인하는 한 번). 넣을 때 정하면 1건과 3건을 번갈아
    가져와 가져오기가 두 배 가까이 되고, 대기 행이 많아 가져오기가 비싸면 쌓인 것을 비우는 속도가 라운드 때보다 느렸다."""
    c = _with(ctx, webhook_max_per_org=10, webhook_batch_size=4)
    for i in range(4):
        _subscribe(c, ORG_A, f"https://1.1.1.1/q{i}")
    _emit(c, ORG_A, 3)  # 구독마다 세 건
    original_record, original_claim = webhooks._record, webhooks._claim
    sizes: list[int] = []

    def slow_record(*args, **kwargs):  # type: ignore[no-untyped-def]
        time.sleep(0.2)  # 기록이 DB 스레드에 줄 선다 (보내기는 바로 끝난다)
        return original_record(*args, **kwargs)

    def counted(*args, **kwargs):  # type: ignore[no-untyped-def]
        out = original_claim(*args, **kwargs)
        sizes.append(len(out[0]))
        return out

    monkeypatch.setattr(webhooks, "_record", slow_record)
    monkeypatch.setattr(webhooks, "_claim", counted)
    assert webhooks.deliver_due(c, send=Receiver())["delivered"] == 12
    assert sizes == [4, 4, 4, 0], sizes  # 고치기 전: [4, 1, 3, 1, 3, 0, …]


def test_claim_error_stops_claiming_and_records_in_flight_before_raising(ctx, monkeypatch) -> None:
    """가져오기가 DB 오류로 실패하면 더 가져오지 않고, 보내던 건을 끝까지 보내 기록한 뒤 그 오류를 올린다
    (전용 스레드가 로그를 남기고 간격만큼 쉰 뒤 다시 한다). 보내던 건을 버리고 바로 올리면 그 건은 SENDING으로 남아
    임대가 끝날 때까지 기다렸다가 다시 나간다."""
    c = _with(ctx, webhook_timeout_s=1.0)
    fast = _subscribe(c, ORG_A, "https://1.1.1.1/fast")
    slow = _subscribe(c, ORG_B, "https://1.1.1.1/slow")
    _emit(c, ORG_A, 2)
    _emit(c, ORG_B, 2)
    original = webhooks._claim
    calls: list[int] = []
    raised = threading.Event()

    def failing(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(len(calls) + 1)
        if len(calls) > 1:
            raised.set()
            raise RuntimeError("synthetic db outage")
        return original(*args, **kwargs)

    def send(url: str, body: bytes, headers: dict[str, str], timeout_s: float) -> int:
        if url.endswith("/slow"):
            assert raised.wait(5)  # 둘째 가져오기가 실패할 때 아직 보내는 중이다
        return 204

    monkeypatch.setattr(webhooks, "_claim", failing)
    with pytest.raises(RuntimeError, match="synthetic db outage"):
        webhooks.deliver_due(c, send=send)
    assert calls == [1, 2]  # 실패한 뒤로는 가져오지 않았다
    for wid in (fast, slow):  # 보내던 건(실패 뒤에 끝난 slow 포함)은 기록했다
        assert sorted((d.status, d.attempts) for d in _deliveries(ctx, webhook_id=wid)) == [("DELIVERED", 1),
                                                                                             ("PENDING", 0)]
    assert _sending_by_org(ctx) == {}


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_disable_notice_failure_is_logged_and_other_deliveries_continue(ctx) -> None:
    """비활성 알림이 실패해도(메일 서버 오류) deliver_due는 정상 결과를 돌려주고 다른 기관의 전송은 모두 나간다.
    실패는 서버 로그 webhook_notify_failed로만 남는다 (예전에는 예외가 deliver_due 밖으로 나갔다)."""
    bad = _subscribe(ctx.c, ORG_A, "https://1.1.1.1/bad")
    _emit(ctx.c, ORG_A)
    good = _subscribe(ctx.c, ORG_B, "https://1.1.1.1/good")
    _emit(ctx.c, ORG_B, 3)

    class BrokenMail:
        def send(self, note: Notification) -> None:
            raise RuntimeError("synthetic smtp outage")

    c = dataclasses.replace(_with(ctx, webhook_max_attempts=1), notifier=BrokenMail())

    def send(url: str, body: bytes, headers: dict[str, str], timeout_s: float) -> int:
        return 500 if url.endswith("/bad") else 204

    logger, handler = logging.getLogger(webhooks.__name__), _Collect()
    logger.addHandler(handler)
    try:
        stats = webhooks.deliver_due(c, send=send)
    finally:
        logger.removeHandler(handler)
    assert stats == {"delivered": 3, "retry": 0, "failed": 1, "cancelled": 0}
    assert _delivered(ctx, good) == 3
    errors = [r for r in handler.records if r.levelno >= logging.ERROR]
    assert [(r.getMessage(), getattr(r, "webhook_id", None)) for r in errors] == [("webhook_notify_failed", bad)]


def test_dispatch_lag_metric_counts_wait_from_due_time(ctx) -> None:
    """디스패처 지연 지표: 기한(next_attempt_at)부터 가져갈 때까지 기다린 시간. 보내는 중 건수는 끝나면 0."""
    wid = _subscribe(ctx.c, ORG_A, PUBLIC_URL)
    _emit(ctx.c, ORG_A, 2)
    now = ctx.c.clock.now()
    with ctx.c.engine.begin() as conn:
        conn.execute(text("UPDATE webhook_deliveries SET next_attempt_at = :t WHERE webhook_id = :w"),
                     {"t": now - timedelta(seconds=40), "w": wid})

    def sample(name: str) -> float:
        return WORKER_REGISTRY.get_sample_value(name) or 0.0

    count, total = sample("webhook_dispatch_lag_seconds_count"), sample("webhook_dispatch_lag_seconds_sum")
    assert webhooks.deliver_due(ctx.c, now=now, send=Receiver())["delivered"] == 2
    assert sample("webhook_dispatch_lag_seconds_count") - count == 2
    assert abs(sample("webhook_dispatch_lag_seconds_sum") - total - 80.0) < 0.01  # 두 건 모두 40초 기다렸다
    assert sample("webhook_deliveries_in_flight") == 0.0
