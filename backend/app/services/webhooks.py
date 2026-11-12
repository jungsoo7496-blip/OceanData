"""기관 웹훅 (04번 요구사항 8, ADR-0030·0041).

- 구독: 이벤트 종류·https URL, 기관당 WEBHOOK_MAX_SUBSCRIPTIONS_PER_ORG개까지. 서명 비밀은 저장하지 않는다 — 서버 키와
  구독 salt로 다시 만든다 (등록 응답에서 한 번만 보임).
- 발행(outbox): 자산 ingest_status·share_status, 이용 권한 status가 바뀌는 같은 트랜잭션에서
  `before_flush`가 보낼 목록을 만든다.
  어느 코드 경로로 바뀌든 빠지지 않는다. 새로 만든 행(파생본 생성 등)은 대상이 아니다.
- 전송 (ADR-0041): 워커의 웹훅 전용 스레드가 라운드를 되풀이한다.
  행 잠금을 잡은 채로 네트워크에 나가지 않는다.
  1) 가져오기 — 짧은 트랜잭션에서 기한이 된 전송을 구독당 1건, 기관당 WEBHOOK_MAX_PER_ORG건,
     모두 WEBHOOK_BATCH_SIZE건까지 고른다(구독·전송 행 모두 SKIP LOCKED라 다른 워커를 기다리지 않는다).
     SENDING(임대)으로 바꾸고 커밋한다.
  2) 보내기 — 트랜잭션 밖에서 동시에. 한 건은 이름 풀기부터 응답 헤더까지 WEBHOOK_TIMEOUT_S 안에
     끝나야 하고 상태 코드만 본다(본문은 읽지 않는다). HMAC-SHA256 서명(`timestamp.body`)·이벤트 ID.
     리디렉션은 따르지 않는다.
  3) 기록 — 건마다 짧은 트랜잭션. 실패는 지수 백오프, WEBHOOK_MAX_ATTEMPTS번 실패하면 구독을 끄고
     기관 ORG_ADMIN에게 알린다(알림은 커밋 뒤). 보내던 워커가 죽으면 임대가 끝난 뒤 다시 보낸다
     (최소 한 번, 수신 쪽은 X-Event-Id로 중복 제거).
- SSRF: https만, 공인(global) 주소로 풀리는 대상만(개발용 설정으로만 예외). 보낼 때는 검사한 주소로 바로 연결하고
  (이름은 Host·TLS SNI·인증서 확인에만), 프록시 환경변수는 따르지 않는다. 이름을 못 푼 것과 공인 주소가 아닌 것은
  기관에 같은 문구로 알린다(자세한 이유는 서버 로그).
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import ssl
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import urlsplit

import httpcore
import httpx
from sqlalchemy import and_, event, exists, func, inspect, select, text, update
from sqlalchemy.orm import Session, aliased

from app.db.models import Asset, Grant, Upload, User, WebhookDelivery, WebhookSubscription
from app.domain.ids import new_ulid
from app.errors import ProblemError
from app.ports.notifier import Notification
from app.timefmt import iso

if TYPE_CHECKING:
    from app.config import Settings
    from app.container import Container

log = logging.getLogger(__name__)
T = TypeVar("T")
EVENT_TYPES = ("upload.registered", "upload.quarantined", "asset.processed", "share.status_changed", "grant.revoked",
               "incident.opened", "incident.resolved")
_BEFORE_REGISTERED = {None, "UPLOADING", "RECEIVED", "VALIDATING", "QUARANTINED"}
_REGISTERED_OR_LATER = {"REGISTERED", "PROXIED", "READY_PRIVATE"}

Sender = Callable[[str, bytes, dict[str, str], float], int]
SENDING = "SENDING"
_DUE = ("PENDING", SENDING)  # 임대(next_attempt_at)가 지난 SENDING은 보내던 워커가 죽은 것 — 다시 보낸다
# 이름을 못 풀었든 공인 주소가 아니든 기관에는 같은 문구 (내부 이름이 있는지 드러나지 않게). 자세한 이유는 서버 로그
REFUSED = "보낼 수 없는 대상입니다 (공인 주소로 풀리는 https 주소만 보냅니다)"


class TargetRefused(Exception):
    """보낼 수 없는 대상. str()은 기관에 보여도 되는 문구, reason은 서버 로그용 (기관에 보이지 않는다)."""

    def __init__(self, message: str, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason or message


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
    """공인(global) 주소가 아니면 막는다 (공유 주소 100.64.0.0/10 포함).

    파이썬이 global로 보는 멀티캐스트 등은 예전 검사로 막는다. IPv4 사상 주소(::ffff:a.b.c.d)는 안의 IPv4로
    판단한다. NAT64(64:ff9b::/96)는 예약, 6to4(2002::/16)는 사설로 막힌다."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _ip_blocked(ip.ipv4_mapped)
    return (not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified)


def _parse(url: str) -> httpx.URL:
    """검사와 전송이 같은 해석기(httpx)를 쓴다 — 해석기 차이로 검사한 이름과 연결하는 이름이 달라지지 않게."""
    try:
        u = httpx.URL(url)
    except (httpx.InvalidURL, TypeError, ValueError):
        raise TargetRefused("URL 형식이 올바르지 않습니다") from None
    if u.scheme != "https" or not u.host:
        raise TargetRefused("https URL만 받습니다")
    if u.userinfo:
        raise TargetRefused("URL에 사용자 정보를 넣을 수 없습니다")
    if u.port is not None and not 0 < u.port < 65536:
        raise TargetRefused("포트가 올바르지 않습니다")
    return u


def _lookup(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    return list(dict.fromkeys(str(i[4][0]) for i in infos))


def check_url(url: str, *, allow_private: bool, resolve: bool) -> list[str]:
    """대상 검사 (등록 때 resolve=False, 보내기 직전 resolve=True). 연결해도 되는 주소 목록을 돌려준다.

    https만, 사용자 정보·내부 이름 금지, IP 리터럴은 공인 주소만. resolve=True면 이름을 풀어 모든 주소를
    검사한다 — 보낼 때는 여기서 돌려준 주소로만 연결한다(DNS 재바인딩 방지).
    allow_private(개발용)면 주소를 검사하지 않는다."""
    u = _parse(url)
    host = u.host.rstrip(".")
    literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if not allow_private:
        if literal is None and (host == "localhost" or host.endswith((".localhost", ".local", ".internal"))):
            raise TargetRefused("내부 주소로는 보내지 않습니다")
        if literal is not None and _ip_blocked(literal):
            raise TargetRefused("공인 주소가 아닌 곳(사설·루프백·공유 주소 등)으로는 보내지 않습니다")
    if literal is not None:
        return [literal.compressed]
    if not resolve:
        return []
    try:
        addresses = _lookup(u.raw_host.decode("ascii"), u.port or 443)
    except (OSError, UnicodeError) as exc:
        raise TargetRefused(REFUSED, f"unresolvable {host}: {type(exc).__name__}") from None
    if not allow_private:
        blocked = [a for a in addresses if _ip_blocked(ipaddress.ip_address(a))]
        if blocked:
            raise TargetRefused(REFUSED, f"non_global {host} -> {blocked[0]}")
    if not addresses:
        raise TargetRefused(REFUSED, f"no_address {host}")
    return addresses


# ------------------------------------------------------------------ 구독


def subscription_view(s: WebhookSubscription) -> dict[str, Any]:
    return {"webhook_id": s.webhook_id, "url": s.url, "event_types": list(s.event_types),
            "active": s.active and s.deleted_at is None, "created_at": iso(s.created_at),
            "disabled_at": iso(s.disabled_at) if s.disabled_at else None, "disabled_reason": s.disabled_reason}


def subscribe(db: Session, settings: Settings, *, organization_id: str, url: str, event_types: list[str], by: str,
              now: datetime) -> tuple[WebhookSubscription, str]:
    check_url(url, allow_private=settings.webhook_allow_private_targets, resolve=False)
    from app.domain.ids import new_id

    # 기관마다 차례로 센다 (동시에 등록해도 상한을 넘지 않게). 지운 구독은 세지 않고, 중지된 구독은 센다
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"webhook_subscribe:{organization_id}"})
    held = db.execute(select(func.count()).select_from(WebhookSubscription).where(
        WebhookSubscription.organization_id == organization_id, WebhookSubscription.deleted_at.is_(None))).scalar_one()
    cap = settings.webhook_max_subscriptions_per_org
    if held >= cap:
        raise ProblemError("CONFLICT", detail=f"웹훅 구독은 기관당 {cap}개까지입니다(중지된 구독 포함). "
                                              "쓰지 않는 구독을 지운 뒤 등록하세요")
    s = WebhookSubscription(webhook_id=new_id("webhook"), organization_id=organization_id, url=url,
                            event_types=sorted(set(event_types)), secret_salt=secrets.token_hex(16), active=True,
                            consecutive_failures=0, created_by=by, created_at=now)
    db.add(s)
    db.flush()
    return s, secret_for(settings, s.secret_salt)


def unsubscribe(db: Session, s: WebhookSubscription, now: datetime) -> int:
    """구독을 지우고 아직 보내지 않은 전송을 취소한다. 보내는 중(SENDING)인 한 건은 결과를 기록할 때 정리한다."""
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


def emit(session: Session, organization_id: str, event_type: str, data: dict[str, Any]) -> int:
    """상태 변경 리스너 밖에서 직접 발행 (예: 11번 장애 열림·해제). 같은 트랜잭션의 outbox에 쌓는다."""
    return _enqueue(session, [(event_type, organization_id, data)])


@event.listens_for(Session, "before_flush")
def _outbox(session: Session, _ctx: Any, _instances: Any) -> None:
    pending = [e for obj in session.dirty for e in _events_for(session, obj)]
    if pending:
        _enqueue(session, pending)


def _enqueue(session: Session, pending: list[tuple[str, str, dict[str, Any]]]) -> int:
    now = datetime.now(UTC)
    added = 0
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
            added += 1
    return added


# ------------------------------------------------------------------ 전송: 네트워크 (트랜잭션 밖)


@lru_cache(maxsize=1)
def _tls_context() -> ssl.SSLContext:
    """인증서 확인을 켠 TLS 설정 (httpx 기본과 같은 CA 목록). 프로세스에서 한 번 만들어 같이 쓴다."""
    return httpx.create_ssl_context()


def _in_thread(fn: Callable[..., T], *args: Any, **kwargs: Any) -> asyncio.Future[T]:
    """취소할 수 없는 동기 호출(getaddrinfo, 시험용 전송 함수)을 데몬 스레드에서 돌린다.

    기한이 지나면 기다리지 않고 버린다(스레드는 혼자 끝난다). 이벤트 루프의 기본 실행기를 쓰지 않는 까닭:
    asyncio.run이 끝날 때 그 스레드들을 기다린다."""
    fut: concurrent.futures.Future[T] = concurrent.futures.Future()

    def run() -> None:
        if not fut.set_running_or_notify_cancel():
            return
        try:
            fut.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 - 기다리는 코루틴으로 그대로 넘긴다
            fut.set_exception(exc)

    threading.Thread(target=run, name="webhook-io", daemon=True).start()
    return asyncio.wrap_future(fut)


class _ClosingBackend(httpcore.AsyncNetworkBackend):
    """연 TCP 연결을 기록해 두는 네트워크 백엔드. 전송이 끝나면(정상·오류·취소) 남은 것을 모두 닫는다 (ADR-0061).

    httpcore는 TLS 핸드셰이크가 일반 예외로 실패할 때만 소켓을 닫고, 전체 기한(asyncio.wait_for)이 핸드셰이크 중에
    닿아 취소(CancelledError)로 끝나면 닫지 않는다 — 그 소켓은 가비지 수집 때까지 열려 있었다(검토 2026-11-12 W3)."""

    def __init__(self) -> None:
        self._inner = httpcore.AnyIOBackend()
        self.streams: list[httpcore.AsyncNetworkStream] = []

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None,
                          socket_options: Any = None) -> httpcore.AsyncNetworkStream:
        stream = await self._inner.connect_tcp(host, port, timeout=timeout, local_address=local_address,
                                               socket_options=socket_options)
        self.streams.append(stream)
        return stream

    async def connect_unix_socket(self, path: str, timeout: float | None = None,
                                  socket_options: Any = None) -> httpcore.AsyncNetworkStream:
        raise httpcore.UnsupportedProtocol("웹훅은 유닉스 소켓으로 보내지 않습니다")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)

    async def close_all(self) -> None:
        for stream in self.streams:  # 정상 종료로 이미 닫힌 것은 그대로 지나간다
            with contextlib.suppress(Exception):
                await stream.aclose()


def _transport(verify: ssl.SSLContext, backend: _ClosingBackend) -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport(verify=verify, trust_env=False)
    # httpx는 네트워크 백엔드를 받는 인자가 없어 연결 풀의 백엔드를 바꾼다. httpx·httpcore를 올렸는데 이 이름이
    # 없어지면 조용히 넘어가지 않고 실패한다 (tests/unit/test_webhook_send.py가 소켓이 닫히는지 본다)
    pool = transport._pool
    if not hasattr(pool, "_network_backend"):
        raise RuntimeError("httpcore 연결 풀에 _network_backend가 없습니다 — 웹훅 전송 소켓 정리를 다시 맞추세요")
    pool._network_backend = backend
    return transport


async def _post(url: str, body: bytes, headers: dict[str, str], timeout: float, *, addresses: list[str],
                verify: ssl.SSLContext | None = None) -> int:
    """검사한 주소로 바로 연결해 보내고 상태 코드만 돌려준다. 응답 본문은 읽지 않고 연결을 닫는다.

    이름은 Host 헤더·TLS SNI·인증서 확인에만 쓰고 다시 풀지 않는다. 프록시 환경변수(HTTPS_PROXY 등)와
    리디렉션은 따르지 않는다. 전체 기한은 부르는 쪽의 asyncio.wait_for가 건다(단계별 timeout은 보조).
    주소가 여럿이면 연결이 안 될 때만 다음 주소로. 연 소켓은 어떻게 끝나든 여기서 닫는다(ADR-0061)."""
    u = httpx.URL(url)
    hdrs = {**headers, "Host": u.netloc.decode("ascii")}
    ext = {"sni_hostname": u.raw_host.decode("ascii")}
    backend = _ClosingBackend()
    try:
        async with httpx.AsyncClient(transport=_transport(verify or _tls_context(), backend), trust_env=False,
                                     follow_redirects=False, timeout=timeout) as client:
            for n, address in enumerate(addresses, 1):
                try:
                    async with client.stream("POST", u.copy_with(host=address), content=body, headers=hdrs,
                                             extensions=ext) as r:
                        return r.status_code
                except httpx.ConnectError:
                    if n == len(addresses):
                        raise
        raise TargetRefused(REFUSED, "no_address")
    finally:
        await backend.close_all()


def http_send(url: str, body: bytes, headers: dict[str, str], timeout: float, *, addresses: list[str] | None = None,
              verify: ssl.SSLContext | None = None) -> int:
    """웹훅 한 건 전송 (동기). 상태 코드만 본다.

    이름 풀기부터 응답 헤더까지 전체를 timeout 안에 끝내지 못하면 TimeoutError. addresses를 주면 그 주소로만
    연결한다(이름은 Host·SNI·인증서 확인에만). 주지 않으면 이름을 풀어 쓰고 대상은 검사하지 않는다 — 기관이 준
    URL은 deliver_due(검사 → 고정)로 보낸다. 실행 중인 이벤트 루프 안에서는 부르지 않는다."""

    async def run() -> int:
        if addresses:
            targets = addresses
        else:
            u = httpx.URL(url)
            targets = await _in_thread(_lookup, u.raw_host.decode("ascii"), u.port or (443 if u.scheme == "https"
                                                                                     else 80))
        return await _post(url, body, headers, timeout, addresses=targets, verify=verify)

    return asyncio.run(asyncio.wait_for(run(), timeout))


# ------------------------------------------------------------------ 전송: 가져오기·기록 (짧은 트랜잭션)


@dataclass(frozen=True)
class _Claim:
    delivery_id: str
    webhook_id: str
    attempt: int  # 가져올 때 올린 시도 번호 — 기록할 때 같아야 이 임대가 아직 내 것이다
    url: str
    body: bytes
    headers: dict[str, str]


def backoff(settings: Settings, attempts: int) -> timedelta:
    return timedelta(seconds=min(settings.webhook_backoff_max_s, settings.webhook_backoff_base_s * 2 ** (attempts - 1)))


def _lease(settings: Settings, batch: int) -> timedelta:
    """가져온 뒤 결과를 기록할 때까지의 임대: 보내기(기한 1번) + 건마다 기록(잠금 대기 ≤ 기한) + 여유(기한 1번)."""
    return timedelta(seconds=settings.webhook_timeout_s * (batch + 2))


def _due(now: datetime) -> Any:
    return and_(WebhookDelivery.status.in_(_DUE), WebhookDelivery.next_attempt_at <= now)


_LIVE = and_(WebhookSubscription.active.is_(True), WebhookSubscription.deleted_at.is_(None))


def _busy(webhook_id: Any, now: datetime) -> Any:
    """임대가 살아 있는 SENDING이 있다 = 다른 워커(또는 앞 라운드)가 이 구독의 한 건을 보내는 중."""
    sending = aliased(WebhookDelivery)
    return exists().where(sending.webhook_id == webhook_id, sending.status == SENDING, sending.next_attempt_at > now)


def _candidates(db: Session, now: datetime, limit: int, per_org: int) -> list[str]:
    """보낼 차례인 구독 (잠그지 않는 조회). 구독마다 가장 오래 기다린 기한으로 줄 세우고 기관마다 앞에서
    per_org개만, 모두 limit개. 보내는 중인 구독은 뺀다 — 잠근 뒤 _claim이 다시 확인한다."""
    first = (select(WebhookDelivery.webhook_id, func.min(WebhookDelivery.next_attempt_at).label("first_due"))
             .where(_due(now)).group_by(WebhookDelivery.webhook_id).subquery())
    rank = func.row_number().over(partition_by=WebhookSubscription.organization_id,
                                  order_by=(first.c.first_due, WebhookSubscription.webhook_id))
    ranked = (select(WebhookSubscription.webhook_id, first.c.first_due, rank.label("rn"))
              .join(first, first.c.webhook_id == WebhookSubscription.webhook_id)
              .where(_LIVE, ~_busy(WebhookSubscription.webhook_id, now)).subquery())
    return list(db.execute(select(ranked.c.webhook_id).where(ranked.c.rn <= per_org)
                           .order_by(ranked.c.first_due, ranked.c.webhook_id).limit(limit)).scalars())


def _claim(c: Container, now: datetime, limit: int) -> tuple[list[_Claim], int]:
    """한 라운드에 보낼 전송을 고르고 SENDING(임대)으로 표시한다. 행 잠금은 모두 SKIP LOCKED라 기다리지 않는다.

    구독당 1건(이미 보내는 중인 구독은 건너뜀), 기관당 WEBHOOK_MAX_PER_ORG건, 모두 limit건. 기한이 오래된 것부터."""
    s = c.settings
    due = _due(now)
    claims: list[_Claim] = []
    with c.sessionmaker.begin() as db:
        # 지웠거나 꺼진 구독에 남은 전송(구독을 끄는 트랜잭션과 겹쳐 쌓인 것, 임대가 끝난 SENDING)은 취소
        dead = (select(WebhookDelivery.delivery_id)
                .join(WebhookSubscription, WebhookSubscription.webhook_id == WebhookDelivery.webhook_id)
                .where(due, ~_LIVE).with_for_update(of=WebhookDelivery, skip_locked=True))
        res = db.execute(update(WebhookDelivery).where(WebhookDelivery.delivery_id.in_(dead))
                         .values(status="CANCELLED"))
        cancelled = int(getattr(res, "rowcount", 0) or 0)
        order = _candidates(db, now, limit, s.webhook_max_per_org)
        if not order:
            return claims, cancelled
        # 구독 행을 SKIP LOCKED로 잠근다: 다른 워커가 고르는 중이거나 API가 지우는 중이면 이번 라운드는 건너뛴다
        subs = {x.webhook_id: x for x in db.execute(select(WebhookSubscription).where(
            WebhookSubscription.webhook_id.in_(order), _LIVE).with_for_update(skip_locked=True)).scalars()}
        lease_until = now + _lease(s, limit)
        ts = int(now.timestamp())
        for wid in order:
            sub = subs.get(wid)
            if sub is None:
                continue
            # 잠근 뒤의 새 조회로 다시 본다: 후보를 고른 뒤 잠그기 전에 다른 워커가 이 구독의 한 건을 가져가
            # 커밋했을 수 있다 (후보 조회는 그 전 스냅숏)
            d = db.execute(select(WebhookDelivery).where(WebhookDelivery.webhook_id == wid, due, ~_busy(wid, now))
                           .order_by(WebhookDelivery.next_attempt_at, WebhookDelivery.delivery_id).limit(1)
                           .with_for_update(of=WebhookDelivery, skip_locked=True)).scalar_one_or_none()
            if d is None:
                continue
            d.status, d.attempts, d.next_attempt_at = SENDING, d.attempts + 1, lease_until
            body = json.dumps(d.payload, ensure_ascii=False, separators=(",", ":")).encode()
            headers = {"Content-Type": "application/json", "X-Signature-Timestamp": str(ts),
                       "X-Signature": sign(secret_for(s, sub.secret_salt), ts, body), "X-Event-Id": d.event_id,
                       "User-Agent": "marine-platform-webhook/1"}
            claims.append(_Claim(d.delivery_id, wid, d.attempts, sub.url, body, headers))
    return claims, cancelled


async def _attempt(cl: _Claim, s: Settings, send: Sender | None) -> tuple[int | None, str | None]:
    """한 건: 이름 풀기·검사 → 검사한 주소로 보내기.

    전체가 WEBHOOK_TIMEOUT_S 안에 끝나야 한다 (넘으면 TimeoutError로 재시도)."""

    async def run() -> int:
        addresses = await _in_thread(check_url, cl.url, allow_private=s.webhook_allow_private_targets, resolve=True)
        if send is not None:  # 시험용 전송 함수
            return await _in_thread(send, cl.url, cl.body, cl.headers, s.webhook_timeout_s)
        return await _post(cl.url, cl.body, cl.headers, s.webhook_timeout_s, addresses=addresses)

    try:
        return await asyncio.wait_for(run(), s.webhook_timeout_s), None
    except TargetRefused as exc:
        log.warning("webhook_target_refused", extra={"webhook_id": cl.webhook_id, "reason": exc.reason})
        return None, f"BLOCKED_TARGET: {exc}"
    except Exception as exc:  # noqa: BLE001 - 네트워크 오류·기한 초과는 재시도
        return None, type(exc).__name__


async def _send_all(claims: list[_Claim], s: Settings, send: Sender | None) -> list[tuple[int | None, str | None]]:
    return list(await asyncio.gather(*(_attempt(cl, s, send) for cl in claims)))


def _disable(db: Session, s: WebhookSubscription, now: datetime, why: str) -> Notification:
    """구독을 끄고 남은 전송을 취소한다. 알림은 돌려주기만 한다 — 부르는 쪽이 커밋한 뒤 보낸다."""
    s.active, s.disabled_at, s.disabled_reason = False, now, why[:300]
    db.execute(update(WebhookDelivery).where(WebhookDelivery.webhook_id == s.webhook_id,
                                             WebhookDelivery.status == "PENDING").values(status="CANCELLED"))
    to = tuple(sorted({e for e in db.execute(select(User.contact_email).where(
        User.organization_id == s.organization_id, User.status == "ACTIVE", User.contact_email.is_not(None),
        User.roles.any("ORG_ADMIN"))).scalars() if e}))  # type: ignore[arg-type]
    host = urlsplit(s.url).hostname or ""
    return Notification(
        kind="webhook.disabled", organization_id=s.organization_id,
        subject="[웹훅 중지] 전송이 계속 실패해 구독을 껐습니다",
        body="\n".join([f"구독: {s.webhook_id} (대상 호스트 {host})", f"이유: {why}", "",
                        "수신 서버를 고친 뒤 구독을 새로 등록하세요. 꺼진 동안의 이벤트는 다시 보내지 않습니다."]),
        recipients=to, data={"webhook_id": s.webhook_id})


def _record(c: Container, cl: _Claim, code: int | None, err: str | None,
            now: datetime) -> tuple[str | None, Notification | None]:
    """결과를 짧은 트랜잭션으로 기록한다. 임대가 이미 다른 워커로 넘어갔거나 취소됐으면 아무것도 바꾸지 않는다."""
    s = c.settings
    with c.sessionmaker.begin() as db:
        # 구독 행은 네트워크에 나가지 않는 짧은 트랜잭션만 잡는다. 그래도 기한보다 오래 기다리지 않는다
        # (넘으면 기록하지 못하고, 임대가 끝난 뒤 다시 보낸다)
        db.execute(text("SELECT set_config('lock_timeout', :v, true)"), {"v": f"{int(s.webhook_timeout_s * 1000)}ms"})
        sub = db.get(WebhookSubscription, cl.webhook_id, with_for_update=True)
        d = db.execute(select(WebhookDelivery).where(
            WebhookDelivery.delivery_id == cl.delivery_id, WebhookDelivery.status == SENDING,
            WebhookDelivery.attempts == cl.attempt).with_for_update()).scalar_one_or_none()
        if sub is None or d is None:
            return None, None
        d.last_status_code, d.last_error = code, err
        if code is not None and 200 <= code < 300:
            d.status, d.delivered_at = "DELIVERED", now
            sub.consecutive_failures = 0
            return "delivered", None
        if not sub.active or sub.deleted_at is not None:  # 보내는 동안 구독을 지웠거나 껐다 — 다시 보내지 않는다
            d.status = "CANCELLED"
            return "cancelled", None
        sub.consecutive_failures += 1
        if d.attempts >= s.webhook_max_attempts:
            d.status = "FAILED"
            return "failed", _disable(db, sub, now, f"{d.attempts}회 실패 (마지막: {err or code})")
        d.status, d.next_attempt_at = "PENDING", now + backoff(s, d.attempts)
        return "retry", None


def deliver_due(c: Container, *, now: datetime | None = None, limit: int | None = None, send: Sender | None = None,
                stop: threading.Event | None = None) -> dict[str, int]:
    """기한이 된 웹훅을 보낸다 (ADR-0041).

    라운드(가져오기 → 트랜잭션 밖에서 동시에 보내기 → 건마다 기록)를 보낼 것이 없을 때까지, 또는 stop이
    설정될 때까지 되풀이한다. 워커 여럿이 동시에 불러도 서로 기다리지 않고 같은 전송을 겹쳐 보내지 않는다.

    now를 주면 모든 시각을 그 값으로 계산한다(시험용). limit은 라운드당 건수(기본 WEBHOOK_BATCH_SIZE).
    send는 시험용 전송 함수 (url, body, headers, timeout) → 상태 코드. 실행 중인 이벤트 루프 안에서는 부르지 않는다."""
    s = c.settings
    stats = {"delivered": 0, "retry": 0, "failed": 0, "cancelled": 0}
    while stop is None or not stop.is_set():
        claims, cancelled = _claim(c, now or c.clock.now(), limit or s.webhook_batch_size)
        stats["cancelled"] += cancelled
        if not claims:
            break
        results = asyncio.run(_send_all(claims, s, send))
        notes: list[Notification] = []
        for cl, (code, err) in zip(claims, results, strict=True):
            try:
                outcome, note = _record(c, cl, code, err, now or c.clock.now())
            except Exception as exc:  # noqa: BLE001 - 기록하지 못한 건은 임대가 끝난 뒤 다시 보낸다 (최소 한 번)
                log.error("webhook_record_failed", exc_info=exc, extra={"delivery_id": cl.delivery_id})
                continue
            if outcome:
                stats[outcome] += 1
            if note:
                notes.append(note)
        for note in notes:  # 라운드의 기록을 모두 커밋한 뒤 (메일 서버가 느려도 임대 안에 기록을 마친다)
            c.notifier.send(note)
    if any(stats.values()):
        log.info("webhooks_dispatched", extra=stats)
    return stats
