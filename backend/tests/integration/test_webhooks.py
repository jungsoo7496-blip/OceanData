"""04번 웹훅 (요구사항 8.1~8.3): 구독 API·SSRF 차단, 상태 변경 → outbox, 서명 전송, 지수 백오프, N회 실패 시 비활성·알림."""

from __future__ import annotations

import dataclasses
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.models import Asset, WebhookDelivery, WebhookSubscription
from app.seed import DEV_A_SENSOR, ORG_A, ORG_B, ORG_CONSUMER, PRJ_B
from app.services import contracts, webhooks
from tests.acceptance.test_t08_t15_sharing import CARD, access, approved_pair, decide, share, to_l3
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_ingest import drain, manifest, upload_all

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
                "https://user:pw@1.1.1.1/x", "https://[::1]/x"):
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
