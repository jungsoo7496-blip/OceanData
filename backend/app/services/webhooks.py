"""기관 웹훅 (04번 요구사항 8, ADR-0030).

- 구독: 이벤트 종류·https URL. 서명 비밀은 저장하지 않는다 — 서버 키와 구독 salt로 다시 만든다
  (등록 응답에서 한 번만 보임).
- 발행(outbox): 자산 ingest_status·share_status, 이용 권한 status가 바뀌는 같은 트랜잭션에서
  `before_flush`가 보낼 목록을 만든다.
  어느 코드 경로로 바뀌든 빠지지 않는다. 새로 만든 행(파생본 생성 등)은 대상이 아니다.
- 전송: 워커 주기마다 기한이 된 것을 SKIP LOCKED로 가져와 HMAC-SHA256 서명(`timestamp.body`)·이벤트 ID를 붙여 보낸다.
  실패는 지수 백오프, WEBHOOK_MAX_ATTEMPTS번 실패하면 구독을 끄고 기관 ORG_ADMIN에게 알린다. 리디렉션은 따르지 않는다.
- SSRF: https만, 사설·루프백·링크로컬 등 주소로 풀리는 대상은 보내지 않는다(개발용 설정으로만 허용).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import event, inspect, select, update
from sqlalchemy.orm import Session

from app.db.models import Asset, Grant, Upload, User, WebhookDelivery, WebhookSubscription
from app.domain.ids import new_ulid
from app.ports.notifier import Notification
from app.timefmt import iso

if TYPE_CHECKING:
    from app.config import Settings
    from app.container import Container

log = logging.getLogger(__name__)
EVENT_TYPES = ("upload.registered", "upload.quarantined", "asset.processed", "share.status_changed", "grant.revoked")
_BEFORE_REGISTERED = {None, "UPLOADING", "RECEIVED", "VALIDATING", "QUARANTINED"}
_REGISTERED_OR_LATER = {"REGISTERED", "PROXIED", "READY_PRIVATE"}

Sender = Callable[[str, bytes, dict[str, str], float], int]


class TargetRefused(Exception):
    pass


# ------------------------------------------------------------------ 비밀·서명


def _key(settings: Settings) -> bytes:
    if settings.webhook_secret_key is not None:
        return settings.webhook_secret_key.get_secret_value().encode()
    # 별도 키가 없으면 기기 토큰 서명 키에서 용도를 나눠 파생한다 (같은 키를 그대로 쓰지 않는다)
    return hmac.new(settings.device_token_signing_key.get_secret_value().encode(), b"webhook-secret-key/v1",
                    hashlib.sha256).digest()


def secret_for(settings: Settings, salt: str) -> str:
    raw = hmac.new(_key(settings), b"webhook-secret/" + salt.encode(), hashlib.sha256).digest()
    return "whsec_" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def sign(secret: str, timestamp: int, body: bytes) -> str:
    return hmac.new(secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256).hexdigest()


# ------------------------------------------------------------------ 대상 검사 (SSRF)


def _ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
            or ip.is_unspecified)


def check_url(url: str, *, allow_private: bool, resolve: bool) -> None:
    """https만, 사용자 정보·사설 주소 금지. resolve=True면 DNS로 풀어 모든 주소를 본다(전송 직전)."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise TargetRefused("https URL만 받습니다")
    if parts.username or parts.password:
        raise TargetRefused("URL에 사용자 정보를 넣을 수 없습니다")
    if allow_private:
        return
    host = parts.hostname
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise TargetRefused("내부 주소로는 보내지 않습니다")
    try:
        if _ip_blocked(ipaddress.ip_address(host)):
            raise TargetRefused("사설·루프백 주소로는 보내지 않습니다")
        return
    except ValueError:
        pass  # 이름이다
    if resolve:
        try:
            infos = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
        except OSError as exc:
            raise TargetRefused(f"이름을 풀 수 없습니다 ({type(exc).__name__})") from None
        if any(_ip_blocked(ipaddress.ip_address(i[4][0])) for i in infos):
            raise TargetRefused("사설·루프백 주소로 풀리는 이름에는 보내지 않습니다")


# ------------------------------------------------------------------ 구독


def subscription_view(s: WebhookSubscription) -> dict[str, Any]:
    return {"webhook_id": s.webhook_id, "url": s.url, "event_types": list(s.event_types),
            "active": s.active and s.deleted_at is None, "created_at": iso(s.created_at),
            "disabled_at": iso(s.disabled_at) if s.disabled_at else None, "disabled_reason": s.disabled_reason}


def subscribe(db: Session, settings: Settings, *, organization_id: str, url: str, event_types: list[str], by: str,
              now: datetime) -> tuple[WebhookSubscription, str]:
    check_url(url, allow_private=settings.webhook_allow_private_targets, resolve=False)
    from app.domain.ids import new_id

    s = WebhookSubscription(webhook_id=new_id("webhook"), organization_id=organization_id, url=url,
                            event_types=sorted(set(event_types)), secret_salt=secrets.token_hex(16), active=True,
                            consecutive_failures=0, created_by=by, created_at=now)
    db.add(s)
    db.flush()
    return s, secret_for(settings, s.secret_salt)


def unsubscribe(db: Session, s: WebhookSubscription, now: datetime) -> int:
    s.deleted_at, s.active = now, False
    res = db.execute(update(WebhookDelivery).where(WebhookDelivery.webhook_id == s.webhook_id,
                                                   WebhookDelivery.status == "PENDING").values(status="CANCELLED"))
    return int(getattr(res, "rowcount", 0) or 0)


# ------------------------------------------------------------------ 발행 (outbox)


def _changed(obj: Any, attr: str) -> tuple[Any, Any] | None:
    hist = inspect(obj).attrs[attr].history
    if not hist.has_changes():
        return None
    old = hist.deleted[0] if hist.deleted else None
    new = getattr(obj, attr)
    return (old, new) if old != new else None


def _events_for(session: Session, obj: Any) -> list[tuple[str, str, dict[str, Any]]]:
    out: list[tuple[str, str, dict[str, Any]]] = []
    if isinstance(obj, Asset):
        ch = _changed(obj, "ingest_status")
        if ch:
            old, new = ch
            if new == "QUARANTINED":
                out.append(("upload.quarantined", obj.organization_id,
                            {"asset_id": obj.asset_id, "ingest_status": new,
                             "quarantine_reason": obj.quarantine_reason or "CHECKSUM_MISMATCH"}))
            elif old in _BEFORE_REGISTERED and new in _REGISTERED_OR_LATER:
                with session.no_autoflush:
                    up = session.execute(select(Upload.upload_id).where(Upload.asset_id == obj.asset_id)
                                         .order_by(Upload.created_at.desc()).limit(1)).scalar_one_or_none()
                if up is not None:  # 메타데이터만 등록 등 업로드가 없는 경로는 대상이 아니다
                    out.append(("upload.registered", obj.organization_id,
                                {"asset_id": obj.asset_id, "upload_id": up, "ingest_status": new}))
            if new == "READY_PRIVATE" and old != "READY_PRIVATE":
                out.append(("asset.processed", obj.organization_id, {"asset_id": obj.asset_id, "ingest_status": new}))
        ch = _changed(obj, "share_status")
        if ch:
            out.append(("share.status_changed", obj.organization_id,
                        {"asset_id": obj.asset_id, "share_status": ch[1]}))
    elif isinstance(obj, Grant):
        ch = _changed(obj, "status")
        if ch and ch[1] == "REVOKED":  # 이용기관(권한을 받은 기관)에게: 새 다운로드 중단·파기 안내
            out.append(("grant.revoked", obj.organization_id, {"grant_id": obj.grant_id}))
    return out


@event.listens_for(Session, "before_flush")
def _outbox(session: Session, _ctx: Any, _instances: Any) -> None:
    pending = [e for obj in session.dirty for e in _events_for(session, obj)]
    if not pending:
        return
    now = datetime.now(UTC)
    subs: dict[str, list[WebhookSubscription]] = {}
    with session.no_autoflush:
        for org in {org for _, org, _ in pending}:
            subs[org] = list(session.execute(select(WebhookSubscription).where(
                WebhookSubscription.organization_id == org, WebhookSubscription.active.is_(True),
                WebhookSubscription.deleted_at.is_(None))).scalars())
    for etype, org, data in pending:
        targets = [s for s in subs.get(org, []) if etype in s.event_types]
        if not targets:
            continue
        event_id = new_ulid()
        payload = {"event_id": event_id, "type": etype, "occurred_at": iso(now), "organization_id": org, "data": data}
        for s in targets:
            session.add(WebhookDelivery(delivery_id=new_ulid(), event_id=event_id, webhook_id=s.webhook_id,
                                        organization_id=org, event_type=etype, payload=payload, status="PENDING",
                                        attempts=0, next_attempt_at=now, created_at=now))


# ------------------------------------------------------------------ 전송


def http_send(url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
    r = httpx.post(url, content=body, headers=headers, timeout=timeout, follow_redirects=False)
    return r.status_code


def backoff(settings: Settings, attempts: int) -> timedelta:
    return timedelta(seconds=min(settings.webhook_backoff_max_s, settings.webhook_backoff_base_s * 2 ** (attempts - 1)))


def _disable(c: Container, db: Session, s: WebhookSubscription, now: datetime, why: str) -> None:
    s.active, s.disabled_at, s.disabled_reason = False, now, why[:300]
    db.execute(update(WebhookDelivery).where(WebhookDelivery.webhook_id == s.webhook_id,
                                             WebhookDelivery.status == "PENDING").values(status="CANCELLED"))
    to = tuple(sorted({e for e in db.execute(select(User.contact_email).where(
        User.organization_id == s.organization_id, User.status == "ACTIVE", User.contact_email.is_not(None),
        User.roles.any("ORG_ADMIN"))).scalars() if e}))  # type: ignore[arg-type]
    host = urlsplit(s.url).hostname or ""
    c.notifier.send(Notification(
        kind="webhook.disabled", organization_id=s.organization_id,
        subject="[웹훅 중지] 전송이 계속 실패해 구독을 껐습니다",
        body="\n".join([f"구독: {s.webhook_id} (대상 호스트 {host})", f"이유: {why}", "",
                        "수신 서버를 고친 뒤 구독을 새로 등록하세요. 꺼진 동안의 이벤트는 다시 보내지 않습니다."]),
        recipients=to, data={"webhook_id": s.webhook_id}))


def deliver_due(c: Container, *, now: datetime | None = None, limit: int = 50,
                send: Sender | None = None) -> dict[str, int]:
    """기한이 된 웹훅을 보낸다. 워커 여럿이 동시에 불러도 SKIP LOCKED로 겹치지 않는다."""
    s = c.settings
    send = send or http_send
    now = now or c.clock.now()
    stats = {"delivered": 0, "retry": 0, "failed": 0, "cancelled": 0}
    with c.sessionmaker.begin() as db:
        rows = list(db.execute(select(WebhookDelivery).where(
            WebhookDelivery.status == "PENDING", WebhookDelivery.next_attempt_at <= now)
            .order_by(WebhookDelivery.next_attempt_at).limit(limit).with_for_update(skip_locked=True)).scalars())
        for d in rows:
            sub = db.get(WebhookSubscription, d.webhook_id, with_for_update=True)
            if sub is None or not sub.active or sub.deleted_at is not None:
                d.status = "CANCELLED"
                stats["cancelled"] += 1
                continue
            body = json.dumps(d.payload, ensure_ascii=False, separators=(",", ":")).encode()
            ts = int(now.timestamp())
            headers = {"Content-Type": "application/json", "X-Signature-Timestamp": str(ts),
                       "X-Signature": sign(secret_for(s, sub.secret_salt), ts, body), "X-Event-Id": d.event_id,
                       "User-Agent": "marine-platform-webhook/1"}
            d.attempts += 1
            code, err = None, None
            try:
                check_url(sub.url, allow_private=s.webhook_allow_private_targets, resolve=True)
                code = send(sub.url, body, headers, s.webhook_timeout_s)
            except TargetRefused as exc:
                err = f"BLOCKED_TARGET: {exc}"
            except Exception as exc:  # noqa: BLE001 - 네트워크 오류는 재시도
                err = type(exc).__name__
            d.last_status_code, d.last_error = code, err
            if code is not None and 200 <= code < 300:
                d.status, d.delivered_at = "DELIVERED", now
                sub.consecutive_failures = 0
                stats["delivered"] += 1
                continue
            sub.consecutive_failures += 1
            if d.attempts >= s.webhook_max_attempts:
                d.status = "FAILED"
                stats["failed"] += 1
                _disable(c, db, sub, now, f"{d.attempts}회 실패 (마지막: {err or code})")
            else:
                d.next_attempt_at = now + backoff(s, d.attempts)
                stats["retry"] += 1
    if any(stats.values()):
        log.info("webhooks_dispatched", extra=stats)
    return stats
