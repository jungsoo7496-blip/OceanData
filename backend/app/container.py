"""의존성 조립. 테스트는 build_container(settings, **overrides)로 어댑터를 바꿔 끼운다."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.keycloak_identity import KeycloakIdentity
from app.adapters.notifier import LogNotifier, SmtpNotifier
from app.adapters.pg_queue import PgQueue
from app.adapters.s3_storage import S3Storage
from app.adapters.system_clock import SystemClock
from app.config import Settings
from app.db.session import make_engine, make_sessionmaker
from app.db.slots import RequestSlots
from app.ports import ClockPort, IdentityPort, NotifierPort, QueuePort, StoragePort
from app.services import sharing as _sharing_guard  # noqa: F401 - 공유 등급 우회 방지 가드 등록 (13번)
from app.services import webhooks as _webhook_outbox  # noqa: F401 - 웹훅 outbox 발행 등록 (04번)
from app.services.audit import AuditLogger
from app.services.auth import Authenticator
from app.services.tokens import MachineTokenIssuer


@dataclass
class Container:
    settings: Settings
    engine: Engine
    sessionmaker: sessionmaker[Session]
    clock: ClockPort
    storage: StoragePort
    identity: IdentityPort
    queue: QueuePort
    notifier: NotifierPort
    audit: AuditLogger
    machine_tokens: MachineTokenIssuer
    authenticator: Authenticator
    # WARM 계층 저장소 (08번 요구사항 9). None이면 HOT 저장소(storage)를 함께 쓰고 버킷만 다르다.
    # 자산 객체를 읽을 때는 app.services.tiers.locate()로 계층·존·키를 정한다
    warm_storage: StoragePort | None = None
    # 요청의 DB 작업 동시 실행 수 (ADR-0062). api.deps.get_db가 쓴다
    db_slots: RequestSlots = field(default_factory=lambda: RequestSlots(15, wait_s=10.0))


def build_container(settings: Settings, **overrides: Any) -> Container:
    engine = overrides.get("engine") or make_engine(
        settings.database_url.get_secret_value(), pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow, pool_timeout=settings.db_pool_timeout_s)
    slots = settings.db_request_slots or max(1, (settings.db_pool_size + settings.db_max_overflow) // 2)
    clock = overrides.get("clock") or SystemClock()
    identity = overrides.get("identity") or KeycloakIdentity(
        settings.idp_issuer_url, settings.idp_audience, internal_url=settings.idp_internal_url,
        mfa_acr_values=settings.idp_mfa_acr_values, timeout_s=settings.readiness_timeout_s)
    machine = MachineTokenIssuer(settings.device_token_signing_key.get_secret_value(), settings.device_token_ttl_s)
    warm = overrides.get("warm_storage") or (
        None if settings.warm_shares_hot_storage else S3Storage(settings.warm_s3_settings()))
    return Container(
        settings=settings, engine=engine, sessionmaker=make_sessionmaker(engine), clock=clock,
        storage=overrides.get("storage") or S3Storage(settings),
        identity=identity,
        queue=overrides.get("queue") or PgQueue(engine, clock),
        notifier=overrides.get("notifier") or (
            SmtpNotifier(settings.smtp_host, settings.smtp_port, settings.mail_from) if settings.smtp_host
            else LogNotifier()),
        audit=AuditLogger(engine, clock),
        machine_tokens=machine,
        authenticator=Authenticator(identity, machine, clock),
        warm_storage=warm,
        db_slots=RequestSlots(slots, wait_s=settings.db_request_slot_wait_s),
    )
