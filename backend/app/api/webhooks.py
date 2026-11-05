"""웹훅 구독 등록·조회·삭제 (04번 요구사항 8.1). 전송은 워커 (services/webhooks.py)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import client_ip, container, get_db, require
from app.container import Container
from app.db.models import WebhookSubscription
from app.domain.principal import Principal
from app.errors import ProblemError
from app.services import webhooks

router = APIRouter(tags=["webhooks"])
EventType = Literal["upload.registered", "upload.quarantined", "asset.processed", "share.status_changed",
                    "grant.revoked", "incident.opened", "incident.resolved"]


class SubscriptionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=500)
    event_types: list[EventType] = Field(min_length=1, max_length=7)


def _own(db: Session, p: Principal) -> list[WebhookSubscription]:
    return list(db.execute(select(WebhookSubscription).where(
        WebhookSubscription.organization_id == p.organization_id, WebhookSubscription.deleted_at.is_(None))
        .order_by(WebhookSubscription.created_at)).scalars())


@router.get("/v1/webhooks/subscriptions")
def list_subscriptions(db: Session = Depends(get_db), p: Principal = Depends(require("webhook:manage"))) -> dict:
    return {"items": [webhooks.subscription_view(s) for s in _own(db, p)]}


@router.post("/v1/webhooks/subscriptions", status_code=201)
def create_subscription(body: SubscriptionCreate, request: Request, db: Session = Depends(get_db),
                        c: Container = Depends(container), p: Principal = Depends(require("webhook:manage"))) -> dict:
    if len(set(body.event_types)) != len(body.event_types):
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/event_types", "code": "unique",
                                                        "message": "이벤트 종류가 중복됐습니다"}])
    try:
        s, secret = webhooks.subscribe(db, c.settings, organization_id=p.organization_id, url=body.url,
                                       event_types=list(body.event_types), by=p.principal_id, now=c.clock.now())
    except webhooks.TargetRefused as exc:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/url", "code": "target",
                                                        "message": str(exc)}]) from None
    c.audit.record("WEBHOOK_SUBSCRIBED", principal=p, organization_id=p.organization_id,
                   target_type="WebhookSubscription", target_id=s.webhook_id, source_ip=client_ip(request),
                   details={"event_types": s.event_types})  # URL·비밀은 감사 로그에 남기지 않는다
    return {**webhooks.subscription_view(s), "secret": secret}


@router.delete("/v1/webhooks/subscriptions/{webhook_id}", status_code=204)
def delete_subscription(webhook_id: str, request: Request, db: Session = Depends(get_db),
                        c: Container = Depends(container),
                        p: Principal = Depends(require("webhook:manage"))) -> Response:
    s = db.execute(select(WebhookSubscription).where(
        WebhookSubscription.webhook_id == webhook_id, WebhookSubscription.organization_id == p.organization_id,
        WebhookSubscription.deleted_at.is_(None)).with_for_update()).scalar_one_or_none()
    if s is None:
        raise ProblemError("NOT_FOUND")
    cancelled = webhooks.unsubscribe(db, s, c.clock.now())
    c.audit.record("WEBHOOK_UNSUBSCRIBED", principal=p, organization_id=p.organization_id,
                   target_type="WebhookSubscription", target_id=s.webhook_id, source_ip=client_ip(request),
                   details={"cancelled_deliveries": cancelled})
    return Response(status_code=204)
