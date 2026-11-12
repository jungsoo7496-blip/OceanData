"""04번 웹훅 (요구사항 8.1~8.3): 구독 API·SSRF 차단, 상태 변경 → outbox, 서명 전송, 지수 백오프, N회 실패 시 비활성·알림.

ADR-0041: 행 잠금 없이 보내기(가져오기·기록만 짧은 트랜잭션), 전체 기한, 구독당 한 건씩, 기관별 공정성, 전용 스레드.
"""

from __future__ import annotations

import dataclasses
import json
import socket
import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from app.db.models import Asset, WebhookDelivery, WebhookSubscription
from app.seed import DEV_A_SENSOR, ORG_A, ORG_B, ORG_CONSUMER, PRJ_B
from app.services import contracts, webhooks
from app.workers import main as worker
from tests.acceptance.test_t08_t15_sharing import CARD, access, approved_pair, decide, share, to_l3
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_ingest import drain, manifest, upload_all
from tests.unit.test_webhook_send import SyntheticReceiver, silent

pytestmark = pytest.mark.integration
ADMIN_A, MEMBER_A, ADMIN_B, OP = 101, 102, 103, 106
PUBLIC_URL = "https://1.1.1.1/marine-hook"  # 공인 주소 리터럴 (시험은 전송 함수를 바꿔 실제로 보내지 않는다)
ALL = ["upload.registered", "upload.quarantined", "asset.processed", "share.status_changed", "grant.revoked"]


class Receiver:
    def __init__(self, codes: list[int] | None = None) -> None:
        self.calls: list[dict] = []
        self.codes = codes or []

    def __call__(self, url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
        self.calls.append({"url": url, "body": body, "headers": headers})
        return self.codes.pop(0) if self.codes else 204


def _deliveries(ctx, **where) -> list[WebhookDelivery]:  # type: ignore[no-untyped-def]
    with ctx.c.sessionmaker() as db:
        q = select(WebhookDelivery).order_by(WebhookDelivery.created_at, WebhookDelivery.event_type)
        for k, v in where.items():
            q = q.where(getattr(WebhookDelivery, k) == v)
        return list(db.execute(q).scalars())


def test_subscription_api_and_target_rules(ictx) -> None:
    ctx = ictx
    admin = ctx.as_user(ADMIN_A)
    path = "/v1/webhooks/subscriptions"
    assert ctx.client.get(path, headers=ctx.as_user(MEMBER_A)).status_code == 403
    for bad in ("http://1.1.1.1/x", "https://127.0.0.1/x", "https://10.0.0.8/x", "https://localhost/x",
                "https://user:pw@1.1.1.1/x", "https://[::1]/x", "https://100.64.0.1/x", "https://[::ffff:10.0.0.8]/x"):
        r = ctx.client.post(path, json={"url": bad, "event_types": ["asset.processed"]}, headers=admin)
        assert r.status_code == 400 and r.json()["errors"][0]["pointer"] == "/url", bad
    assert ctx.client.post(path, json={"url": PUBLIC_URL, "event_types": ["nope"]}, headers=admin).status_code == 400
    r = ctx.client.post(path, json={"url": PUBLIC_URL, "event_types": ["asset.processed"]}, headers=admin)
    check(r, "POST", "/v1/webhooks/subscriptions")
    sub = r.json()
    assert r.status_code == 201 and sub["secret"].startswith("whsec_") and sub["active"] is True
    r = ctx.client.get(path, headers=admin)
    check(r, "GET", "/v1/webhooks/subscriptions")
    assert [x["webhook_id"] for x in r.json()["items"]] == [sub["webhook_id"]] and "secret" not in r.json()["items"][0]
    assert ctx.client.get(path, headers=ctx.as_user(ADMIN_B)).json()["items"] == []  # 기관 격리
    assert ctx.client.delete(f"{path}/{sub['webhook_id']}", headers=ctx.as_user(ADMIN_B)).status_code == 404
    r = ctx.client.delete(f"{path}/{sub['webhook_id']}", headers=admin)
    check(r, "DELETE", "/v1/webhooks/subscriptions/{webhook_id}")
    assert r.status_code == 204 and ctx.client.get(path, headers=admin).json()["items"] == []
    assert ctx.client.delete(f"{path}/{sub['webhook_id']}", headers=admin).status_code == 404
    actions = ctx.audit_actions()
    assert "WEBHOOK_SUBSCRIBED" in actions and "WEBHOOK_UNSUBSCRIBED" in actions and sub["secret"] not in str(actions)
    # 이름은 전송 직전에 풀어 사설 주소면 막는다 (DNS 재바인딩 일부 완화)
    with pytest.raises(webhooks.TargetRefused):
        webhooks.check_url("https://hooks.internal/x", allow_private=False, resolve=False)


def test_events_signed_delivery_retry_and_disable(ictx, monkeypatch) -> None:
    ctx = ictx
    r = ctx.client.post("/v1/webhooks/subscriptions", json={"url": PUBLIC_URL, "event_types": ALL},
                        headers=ctx.as_user(ADMIN_A))
    sub, secret = r.json(), r.json()["secret"]

    # 상태가 바뀌는 같은 트랜잭션에서 outbox에 쌓인다: 등록(→ 비영상은 바로 READY_PRIVATE)·격리·공유 단계
    csv = b"observed_at,value\n2026-11-01T00:00:00Z,18.4\n"
    up = upload_all(ctx, manifest(csv, source=DEV_A_SENSOR, data_type="SENSOR", variant="ORIGINAL",
                                  mime="text/csv"), csv, ctx.as_user(MEMBER_A))
    bad = upload_all(ctx, manifest(b"x" * 1000, sha="0" * 64), b"y" * 1000, ctx.as_user(MEMBER_A))
    drain(ctx)
    with ctx.c.sessionmaker.begin() as db:
        db.get(Asset, up["asset_id"]).data_role = "BYPRODUCT"
    assert ctx.client.post("/v1/share-candidates", json={"asset_ids": [up["asset_id"]]},
                           headers=ctx.as_user(MEMBER_A)).status_code == 200
    kinds = [(d.event_type, d.payload["data"].get("asset_id")) for d in _deliveries(ctx)]
    assert ("upload.registered", up["asset_id"]) in kinds and ("asset.processed", up["asset_id"]) in kinds
    assert ("upload.quarantined", bad["asset_id"]) in kinds and ("share.status_changed", up["asset_id"]) in kinds
    for d in _deliveries(ctx):  # 본문은 계약(webhook-event)대로
        assert contracts.schema_problems("webhook-event", d.payload) == [], d.payload

    # 서명 전송: X-Signature = HMAC-SHA256(secret, timestamp + "." + body), X-Event-Id = 본문 event_id
    rx = Receiver()
    stats = webhooks.deliver_due(ctx.c, send=rx)
    assert stats["delivered"] == len(kinds) == len(rx.calls)
    for call in rx.calls:
        h, body = call["headers"], call["body"]
        assert h["X-Signature"] == webhooks.sign(secret, int(h["X-Signature-Timestamp"]), body)
        assert h["X-Event-Id"] == json.loads(body)["event_id"] and call["url"] == PUBLIC_URL
    assert webhooks.deliver_due(ctx.c, send=rx)["delivered"] == 0  # 한 번만

    # 실패 → 지수 백오프 → N회 실패하면 구독을 끄고 기관 ORG_ADMIN에게 알림, 남은 전송 취소
    c = dataclasses.replace(ctx.c, settings=ctx.c.settings.model_copy(update={"webhook_max_attempts": 3}))
    with ctx.c.sessionmaker.begin() as db:
        db.get(Asset, up["asset_id"]).share_status = "NONE"  # 이벤트 하나 더
        db.get(Asset, bad["asset_id"]).data_role = "BYPRODUCT"
    with ctx.c.sessionmaker.begin() as db:
        db.get(Asset, up["asset_id"]).share_status = "CANDIDATE"
    now = ctx.c.clock.now()
    down = Receiver([500, 503, 500, 500, 500, 500])
    webhooks.deliver_due(c, now=now, send=down)
    pending = _deliveries(ctx, status="PENDING")
    assert len(pending) == 2 and all(d.attempts == 1 and d.last_status_code in (500, 503) for d in pending)
    assert all(abs((d.next_attempt_at - now).total_seconds() - 30) < 1 for d in pending)
    assert webhooks.deliver_due(c, now=now + timedelta(seconds=10), send=down)["retry"] == 0  # 아직 기한 전
    webhooks.deliver_due(c, now=now + timedelta(seconds=31), send=down)
    assert all(abs((d.next_attempt_at - now).total_seconds() - 91) < 1 for d in _deliveries(ctx, status="PENDING"))
    webhooks.deliver_due(c, now=now + timedelta(seconds=92), send=down)
    assert _deliveries(ctx, status="PENDING") == []
    assert len(_deliveries(ctx, status="FAILED")) >= 1
    r = ctx.client.get("/v1/webhooks/subscriptions", headers=ctx.as_user(ADMIN_A))
    assert r.json()["items"][0]["active"] is False and "3회 실패" in r.json()["items"][0]["disabled_reason"]
    note = next(n for n in ctx.c.notifier.sent if n.kind == "webhook.disabled")
    assert note.organization_id == ORG_A and note.recipients and "1.1.1.1" in note.body and secret not in note.body
    with ctx.c.sessionmaker.begin() as db:  # 꺼진 구독에는 더 쌓지 않는다
        db.get(Asset, up["asset_id"]).share_status = "NONE"
    assert _deliveries(ctx, status="PENDING") == []

    # 사설 주소로 풀리는 이름은 전송 직전에 막는다 (실패로 센다)
    with ctx.c.sessionmaker.begin() as db:
        s2, _ = webhooks.subscribe(db, ctx.c.settings, organization_id=ORG_A, url="https://hooks.example.org/x",
                                   event_types=["share.status_changed"], by="t", now=now)
    with ctx.c.sessionmaker.begin() as db:
        db.get(Asset, up["asset_id"]).share_status = "CANDIDATE"
    monkeypatch.setattr(webhooks.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.1.2.3", 443))])
    never = Receiver()
    assert webhooks.deliver_due(ctx.c, send=never)["retry"] == 1 and never.calls == []
    blocked = _deliveries(ctx, webhook_id=s2.webhook_id)[0]
    assert blocked.last_error.startswith("BLOCKED_TARGET")
    with ctx.c.sessionmaker() as db:
        assert db.get(WebhookSubscription, sub["webhook_id"]).consecutive_failures >= 3


def test_grant_revoked_goes_to_consumer_org(ictx) -> None:
    ctx = ictx
    with ctx.c.sessionmaker.begin() as db:
        webhooks.subscribe(db, ctx.c.settings, organization_id=ORG_CONSUMER, url=PUBLIC_URL,
                           event_types=["grant.revoked"], by="t", now=ctx.c.clock.now())
    from app.seed import DEV_B_CAMERA

    b = approved_pair(ctx, ORG_B, PRJ_B, DEV_B_CAMERA, ["AI_TRAINING"])
    share(ctx, ADMIN_B, b["deid"], "SANDBOX_PARTNERS")
    ds = ctx.client.post("/v1/datasets", json={"title": "웹훅 (합성)"}, headers=ctx.as_user(OP)).json()["dataset_id"]
    vid = ctx.client.post(f"/v1/datasets/{ds}/versions", headers=ctx.as_user(OP), json={
        "version": "1.0.0", "item_asset_ids": [b["deid"]], "data_card": CARD}).json()["dataset_version_id"]
    to_l3(ctx, ADMIN_B, b["deid"])
    ctx.client.post(f"/v1/datasets/{ds}/publish", headers=ctx.as_user(OP))
    rid = access(ctx, vid, "AI_TRAINING").json()["access_request_id"]
    decide(ctx, ADMIN_B, rid, "APPROVE")
    grant = decide(ctx, OP, rid, "APPROVE").json()["grant_id"]
    assert _deliveries(ctx, event_type="grant.revoked") == []
    assert ctx.client.post(f"/v1/grants/{grant}/revoke", json={"reason": "시험 철회"},
                           headers=ctx.as_user(OP)).status_code == 200
    d = _deliveries(ctx, event_type="grant.revoked")
    assert [(x.organization_id, x.payload["data"]["grant_id"]) for x in d] == [(ORG_CONSUMER, grant)]
    assert contracts.schema_problems("webhook-event", d[0].payload) == []


def test_incident_events_to_org_webhook(ictx) -> None:
    """11번 요구사항 3.1: 기관 대상 경보 규칙의 장애 열림·해제를 기관 웹훅으로 (재알림은 보내지 않는다)."""
    from app.db.models import Incident, WatchRule
    from app.domain.ids import new_id
    from app.seed import DEV_A_COLLECTOR
    from app.services import monitoring

    ctx = ictx
    r = ctx.client.post("/v1/webhooks/subscriptions", headers=ctx.as_user(ADMIN_A),
                        json={"url": PUBLIC_URL, "event_types": ["incident.opened", "incident.resolved"]})
    check(r, "POST", "/v1/webhooks/subscriptions")
    now = ctx.c.clock.now()
    with ctx.c.sessionmaker.begin() as db:
        rule = db.execute(select(WatchRule).where(WatchRule.notify_org.is_(True))).scalars().first()
        assert rule is not None
        inc = Incident(incident_id=new_id("incident"), alert_key="t", rule_code=rule.rule_code, severity="WARNING",
                       status="OPEN", organization_id=ORG_A, device_id=DEV_A_COLLECTOR, session_id=None,
                       summary="합성 장애", guidance="합성 안내", opened_at=now)
        db.add(inc)
        db.flush()
        for kind in ("OPENED", "REMINDER", "RESOLVED"):
            monitoring.dispatch(ctx.c, db, rule, inc, kind, now)
        iid = inc.incident_id
    got = [(d.event_type, d.payload["data"]["incident_id"]) for d in _deliveries(ctx, organization_id=ORG_A)]
    assert got == [("incident.opened", iid), ("incident.resolved", iid)]
    for d in _deliveries(ctx, organization_id=ORG_A):
        assert contracts.schema_problems("webhook-event", d.payload) == [], d.payload
    # 운영자 전용 경보(기관 장애가 아님)는 기관 웹훅으로 보내지 않는다
    with ctx.c.sessionmaker.begin() as db:
        rule = db.execute(select(WatchRule).where(WatchRule.notify_org.is_(True))).scalars().first()
        op_inc = Incident(incident_id=new_id("incident"), alert_key="op", rule_code=rule.rule_code, severity="CRITICAL",
                          status="OPEN", organization_id=None, summary="운영 장애", guidance="-", opened_at=now)
        db.add(op_inc)
        db.flush()
        monitoring.dispatch(ctx.c, db, rule, op_inc, "OPENED", now)
    assert len(_deliveries(ctx)) == 2


# ------------------------------------------------------------------ ADR-0041: 잠금 없이 보내기·전체 기한·공정성·전용 스레드


def _subscribe(c, org: str, url: str) -> str:  # type: ignore[no-untyped-def]
    with c.sessionmaker.begin() as db:
        s, _ = webhooks.subscribe(db, c.settings, organization_id=org, url=url, event_types=["incident.opened"],
                                  by="t", now=c.clock.now())
        return s.webhook_id


def _emit(c, org: str, n: int = 1) -> None:  # type: ignore[no-untyped-def]
    for i in range(n):  # 따로 커밋해 기한(next_attempt_at)이 생긴 순서대로
        with c.sessionmaker.begin() as db:
            webhooks.emit(db, org, "incident.opened", {"incident_id": f"inc_synthetic_{i}"})


def _with(ctx, **settings):  # type: ignore[no-untyped-def]
    return dataclasses.replace(ctx.c, settings=ctx.c.settings.model_copy(update=settings))


def _no_row_locks(ctx, webhook_id: str) -> None:  # type: ignore[no-untyped-def]
    """다른 연결에서 NOWAIT로 잠가 본다 — 누가 구독·전송 행을 잠그고 있으면 바로 오류(55P03)가 난다."""
    with ctx.c.engine.begin() as conn:
        conn.execute(text("SELECT 1 FROM webhook_subscriptions WHERE webhook_id = :w FOR UPDATE NOWAIT"),
                     {"w": webhook_id})
        conn.execute(text("SELECT 1 FROM webhook_deliveries WHERE webhook_id = :w FOR UPDATE NOWAIT"),
                     {"w": webhook_id})


def test_slow_receiver_does_not_hold_other_workers(ctx) -> None:
    """검토 재현(워커 둘): 느린 수신 서버로 보내는 동안 행 잠금이 없고, 다른 워커는 기다리지 않고 다른 구독을 보낸다.
    같은 구독의 다음 건은 앞 건이 끝난 뒤에 보낸다 (구독당 한 건씩)."""
    slow = _subscribe(ctx.c, ORG_A, "https://1.1.1.1/slow")  # 공인 IP 리터럴: DNS 없이 검사 통과 (실제로는 안 보냄)
    _subscribe(ctx.c, ORG_B, "https://1.1.1.1/fast")
    _emit(ctx.c, ORG_A, 2)
    _emit(ctx.c, ORG_B)
    lock, sending = threading.Lock(), threading.Event()
    in_flight = {"now": 0, "peak": 0}

    def send(url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
        if url.endswith("/slow"):
            with lock:
                in_flight["now"] += 1
                in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
            sending.set()
            time.sleep(1.5)
            with lock:
                in_flight["now"] -= 1
        return 204

    out: dict[str, tuple[float, dict[str, int]]] = {}

    def run(name: str, limit: int) -> None:
        t0 = time.monotonic()
        stats = webhooks.deliver_due(ctx.c, limit=limit, send=send)
        out[name] = (time.monotonic() - t0, stats)

    w1 = threading.Thread(target=run, args=("w1", 1))
    w1.start()
    assert sending.wait(5)  # 워커 1이 느린 구독의 첫 건을 보내는 중
    _no_row_locks(ctx, slow)  # 보내는 동안 구독·전송 행 잠금이 없다
    run("w2", 5)
    w1.join(10)
    assert out["w2"][0] < 1.0 and out["w2"][1]["delivered"] == 1  # 기다리지 않고 다른 기관 구독만 보냈다
    assert out["w1"][1]["delivered"] == 2 and out["w1"][0] >= 3.0  # 느린 구독의 두 건은 차례로
    assert in_flight["peak"] == 1
    assert {d.status for d in _deliveries(ctx)} == {"DELIVERED"}


def test_real_send_is_bounded_and_holds_no_row_locks(ctx) -> None:
    """실제 전송 경로(주입 없음): TLS 핸드셰이크에 답하지 않는 수신 서버도 WEBHOOK_TIMEOUT_S에 끊고 재시도로 돌린다."""
    rx = SyntheticReceiver(silent)
    try:
        c = _with(ctx, webhook_allow_private_targets=True, webhook_timeout_s=1.0)  # 127.0.0.1 합성 서버 (개발용 설정)
        wid = _subscribe(c, ORG_A, f"https://127.0.0.1:{rx.port}/hook")
        _emit(c, ORG_A)
        done: dict[str, object] = {}

        def run() -> None:
            done["stats"] = webhooks.deliver_due(c)
            done["at"] = time.monotonic()

        t0 = time.monotonic()
        t = threading.Thread(target=run)
        t.start()
        assert rx.accepted.wait(5)
        _no_row_locks(ctx, wid)  # 실제 네트워크 대기 중에도 행 잠금이 없다
        t.join(10)
        assert done["at"] - t0 < 1.0 + 1.0  # type: ignore[operator]  # 기한 + DB 기록 여유
        assert done["stats"] == {"delivered": 0, "retry": 1, "failed": 0, "cancelled": 0}
        d = _deliveries(ctx, webhook_id=wid)[0]
        assert (d.status, d.attempts, d.last_error, d.last_status_code) == ("PENDING", 1, "TimeoutError", None)
    finally:
        rx.close()


def test_live_lease_blocks_subscription_and_expired_lease_is_resent(ctx) -> None:
    """보내던 워커가 죽은 경우: 임대가 살아 있으면 그 구독은 건너뛰고(한 건씩), 임대가 끝나면 다시 보낸다 (최소 한 번)."""
    wid = _subscribe(ctx.c, ORG_A, PUBLIC_URL)
    _emit(ctx.c, ORG_A, 2)
    first = _deliveries(ctx, webhook_id=wid)[0]
    now = ctx.c.clock.now()
    with ctx.c.sessionmaker.begin() as db:
        d = db.get(WebhookDelivery, first.delivery_id)
        d.status, d.attempts, d.next_attempt_at = "SENDING", 1, now + timedelta(seconds=60)
    rx = Receiver()
    assert webhooks.deliver_due(ctx.c, send=rx) == {"delivered": 0, "retry": 0, "failed": 0, "cancelled": 0}
    assert rx.calls == []  # 다른 워커가 보내는 중인 구독의 다음 건도 보내지 않는다
    with ctx.c.sessionmaker.begin() as db:
        db.get(WebhookDelivery, first.delivery_id).next_attempt_at = now - timedelta(seconds=1)  # 임대 만료
    assert webhooks.deliver_due(ctx.c, send=rx)["delivered"] == 2
    got = {d.delivery_id: d for d in _deliveries(ctx, webhook_id=wid)}
    assert got[first.delivery_id].attempts == 2 and {d.status for d in got.values()} == {"DELIVERED"}
    assert json.loads(rx.calls[0]["body"])["event_id"] == first.event_id  # 오래 기다린 것부터


def test_claim_rechecks_after_lock_when_another_worker_won(ctx, monkeypatch) -> None:
    """경합: 워커 B가 후보를 고른 뒤 잠그기 전에 워커 A가 같은 구독의 한 건을 가져가 커밋하면, B는 잠근 뒤 다시 보고
    그 구독의 다음 건을 가져가지 않는다 (구독당 한 건씩)."""
    wid = _subscribe(ctx.c, ORG_A, PUBLIC_URL)
    _emit(ctx.c, ORG_A, 2)
    original, won = webhooks._candidates, []

    def racing(db, now, limit, per_org):  # type: ignore[no-untyped-def]
        found = original(db, now, limit, per_org)  # B는 아무도 보내지 않는 상태를 봤다
        if not won:
            won.append(None)  # A 안에서는 끼어들지 않는다
            a = threading.Thread(target=lambda: won.extend(webhooks._claim(ctx.c, now, 5)[0]))
            a.start()
            a.join(10)
        return found

    monkeypatch.setattr(webhooks, "_candidates", racing)
    claims, _ = webhooks._claim(ctx.c, ctx.c.clock.now(), 5)
    assert wid in [c.webhook_id for c in won[1:]] and claims == []
    assert [d.status for d in _deliveries(ctx, webhook_id=wid)] == ["SENDING", "PENDING"]


def test_delete_while_sending_is_not_blocked(ctx) -> None:
    """보내는 동안 구독을 지워도 API가 잠금을 기다리지 않는다 (예전에는 전송 트랜잭션이 구독 행을 잡고 있었다).
    그 한 건이 실패하면 재시도하지 않고 취소한다."""
    r = ctx.client.post("/v1/webhooks/subscriptions", headers=ctx.as_user(ADMIN_A),
                        json={"url": PUBLIC_URL, "event_types": ["incident.opened"]})
    wid = r.json()["webhook_id"]
    _emit(ctx.c, ORG_A)
    seen: dict[str, float] = {}

    def send(url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
        t0 = time.monotonic()
        seen["status"] = ctx.client.delete(f"/v1/webhooks/subscriptions/{wid}",
                                           headers=ctx.as_user(ADMIN_A)).status_code
        seen["seconds"] = time.monotonic() - t0
        return 503

    stats = webhooks.deliver_due(ctx.c, send=send)
    assert seen["status"] == 204 and seen["seconds"] < 2.0
    assert stats == {"delivered": 0, "retry": 0, "failed": 0, "cancelled": 1}
    d = _deliveries(ctx, webhook_id=wid)[0]
    assert (d.status, d.attempts, d.last_status_code) == ("CANCELLED", 1, 503)


def test_round_is_shared_fairly_between_orgs(ctx) -> None:
    """한 라운드는 구독당 1건, 기관당 WEBHOOK_MAX_PER_ORG건, 모두 WEBHOOK_BATCH_SIZE건 — 한 기관이 다 차지하지 못한다."""
    c = _with(ctx, webhook_max_per_org=2, webhook_batch_size=3)
    for i in range(3):
        _subscribe(c, ORG_A, f"https://1.1.1.1/a{i}")
    _subscribe(c, ORG_B, "https://1.1.1.1/b0")
    _emit(c, ORG_A, 2)  # 기관 A가 더 오래 기다렸다 (구독마다 2건)
    _emit(c, ORG_B)
    order: list[str] = []
    lock = threading.Lock()

    def send(url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
        with lock:
            order.append(url.rsplit("/", 1)[1])
        return 204

    assert webhooks.deliver_due(c, send=send)["delivered"] == 7
    first_round = order[:3]  # 다음 라운드는 앞 라운드 기록이 끝난 뒤 시작한다
    assert "b0" in first_round and sum(x.startswith("a") for x in first_round) == 2  # 기관 A는 2건까지
    assert len(set(first_round)) == 3  # 구독당 1건


def test_refused_targets_share_one_reason(ctx, monkeypatch) -> None:
    """이름을 못 푼 대상과 사설·공유(100.64/10) 주소로 풀리는 대상은 기관에 같은 이유로 보인다 (자세한 건 서버 로그)."""
    c = _with(ctx, webhook_max_attempts=1)
    answers = {"nx.example.org": None, "intranet.example.org": "10.0.0.5", "cgnat.example.org": "100.64.0.1"}

    def getaddrinfo(host: str, port: int, *_a: object, **_k: object) -> list:
        if answers.get(host) is None:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers[host], port))]

    monkeypatch.setattr(webhooks.socket, "getaddrinfo", getaddrinfo)
    ids = [_subscribe(c, ORG_A, f"https://{name}/hook") for name in answers]
    _emit(c, ORG_A)
    never = Receiver()
    assert webhooks.deliver_due(c, send=never)["failed"] == 3 and never.calls == []
    items = {x["webhook_id"]: x for x in ctx.client.get("/v1/webhooks/subscriptions",
                                                         headers=ctx.as_user(ADMIN_A)).json()["items"]}
    assert {items[w]["disabled_reason"] for w in ids} == {f"1회 실패 (마지막: BLOCKED_TARGET: {webhooks.REFUSED})"}
    notes = [n for n in ctx.c.notifier.sent if n.kind == "webhook.disabled"]
    assert len(notes) == 3 and len({n.body.splitlines()[1] for n in notes}) == 1  # "이유:" 줄이 같다


def test_cross_org_delete_is_hidden_and_audited(ctx) -> None:
    """다른 기관 구독 삭제: 404로 존재를 숨기고 CROSS_ORG_ACCESS_ATTEMPT를 감사 로그에 남긴다 (security.md)."""
    from app.domain.ids import new_id

    path = "/v1/webhooks/subscriptions"
    wid = ctx.client.post(path, headers=ctx.as_user(ADMIN_A),
                          json={"url": PUBLIC_URL, "event_types": ["asset.processed"]}).json()["webhook_id"]
    r = ctx.client.delete(f"{path}/{wid}", headers=ctx.as_user(ADMIN_B))
    check(r, "DELETE", "/v1/webhooks/subscriptions/{webhook_id}")
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"
    assert ctx.client.delete(f"{path}/{new_id('webhook')}", headers=ctx.as_user(ADMIN_B)).status_code == 404
    with ctx.c.engine.connect() as conn:
        rows = conn.execute(text("SELECT organization_id, actor_organization_id, target_type, target_id, result "
                                 "FROM audit_log WHERE action = 'CROSS_ORG_ACCESS_ATTEMPT'")).all()
    assert [tuple(x) for x in rows] == [(ORG_A, ORG_B, "WebhookSubscription", wid, "DENIED")]  # 없는 ID는 해당 없음
    assert [x["webhook_id"] for x in ctx.client.get(path, headers=ctx.as_user(ADMIN_A)).json()["items"]] == [wid]


def test_subscription_cap_per_org(ctx) -> None:
    """기관당 구독(중지 포함, 지운 것 제외)은 WEBHOOK_MAX_SUBSCRIPTIONS_PER_ORG개까지 — 넘으면 409 CONFLICT (계약)."""
    ctx.c.settings = ctx.c.settings.model_copy(update={"webhook_max_subscriptions_per_org": 2})
    path, admin = "/v1/webhooks/subscriptions", ctx.as_user(ADMIN_A)
    body = {"url": PUBLIC_URL, "event_types": ["asset.processed"]}
    ids = [ctx.client.post(path, headers=admin, json=body).json()["webhook_id"] for _ in range(2)]
    with ctx.c.sessionmaker.begin() as db:  # 중지된 구독도 센다
        db.get(WebhookSubscription, ids[0]).active = False
    r = ctx.client.post(path, headers=admin, json=body)
    check(r, "POST", path)
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"
    assert ctx.client.post(path, headers=ctx.as_user(ADMIN_B), json=body).status_code == 201  # 기관마다 따로 센다
    assert ctx.client.delete(f"{path}/{ids[0]}", headers=admin).status_code == 204  # 지우면 자리가 난다
    assert ctx.client.post(path, headers=admin, json=body).status_code == 201


def test_worker_jobs_run_while_a_webhook_receiver_hangs(ctx) -> None:
    """검토 재현(작업 루프): 수신 서버가 응답하지 않는 동안에도 같은 워커 프로세스의 작업은 바로 처리된다 (전용 스레드)."""
    rx = SyntheticReceiver(silent)
    stop = threading.Event()
    dispatcher: threading.Thread | None = None
    worker.register("test_webhook_noop", lambda c, job: None)
    try:
        ctx.c.settings = ctx.c.settings.model_copy(update={"webhook_allow_private_targets": True,
                                                           "webhook_timeout_s": 2.0})
        wid = _subscribe(ctx.c, ORG_A, f"https://127.0.0.1:{rx.port}/hook")
        _emit(ctx.c, ORG_A)
        dispatcher = worker.start_webhook_dispatcher(ctx.c, stop)
        assert rx.accepted.wait(5)  # 전용 스레드가 응답 없는 수신 서버로 보내는 중
        jid = ctx.c.queue.enqueue("test_webhook_noop", {})
        t0 = time.monotonic()
        assert worker.run_once(ctx.c, "w") and time.monotonic() - t0 < 1.0
        with ctx.c.engine.connect() as conn:
            assert conn.execute(text("SELECT status FROM jobs WHERE job_id = :j"), {"j": jid}).scalar() == "DONE"
        until = time.monotonic() + 5
        while _deliveries(ctx, webhook_id=wid)[0].last_error is None and time.monotonic() < until:
            time.sleep(0.1)
        d = _deliveries(ctx, webhook_id=wid)[0]
        assert (d.status, d.last_error) == ("PENDING", "TimeoutError")  # 기한에 끊고 재시도로
    finally:
        stop.set()
        if dispatcher is not None:
            dispatcher.join(10)
            assert not dispatcher.is_alive()
        worker.HANDLERS.pop("test_webhook_noop", None)
        rx.close()
