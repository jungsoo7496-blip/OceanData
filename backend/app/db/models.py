"""SQLAlchemy 모델. 컬럼 이름은 snake_case, 시각은 UTC timestamptz (structure.md 3장)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import Geometry, Vector

TS = DateTime(timezone=True)
ID = String(30)


class Organization(Base):
    __tablename__ = "organizations"
    organization_id: Mapped[str] = mapped_column(ID, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    org_type: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), server_default="ACTIVE")
    # 기관 정책: 업로드 무결성 검증 때 악성코드 검사를 요구하는가 (04번 요구사항 4.7)
    malware_scan_required: Mapped[bool] = mapped_column(Boolean, server_default="false")
    created_at: Mapped[datetime] = mapped_column(TS)


class User(Base):
    __tablename__ = "users"
    user_id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.organization_id"), index=True)
    idp_subject: Mapped[str] = mapped_column(String(128), unique=True)
    username: Mapped[str] = mapped_column(String(120))
    # 알림 수신 주소 (11번 요구사항 3). 알림 외 용도로 쓰지 않고 알림 본문에도 넣지 않는다
    contact_email: Mapped[str | None] = mapped_column(String(254))
    roles: Mapped[list[str]] = mapped_column(ARRAY(String(30)))
    status: Mapped[str] = mapped_column(String(20), server_default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(TS)
    updated_at: Mapped[datetime] = mapped_column(TS)


class Project(Base):
    __tablename__ = "projects"
    project_id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.organization_id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), server_default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(TS)


class ProjectAssignment(Base):
    """관리기관 열람자(PROGRAM_VIEWER)·검수자 배정 (domain.md 2장)."""

    __tablename__ = "project_assignments"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TS)


class Device(Base):
    __tablename__ = "devices"
    device_id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.organization_id"), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.project_id"))
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20))  # SOURCE / COLLECTOR
    device_type: Mapped[str] = mapped_column(String(30))
    collector_device_id: Mapped[str | None] = mapped_column(ForeignKey("devices.device_id"))
    status: Mapped[str] = mapped_column(String(30), server_default="PENDING_ENROLLMENT")
    collection_mode: Mapped[str] = mapped_column(String(30), server_default="C1_SAMPLE_PREVIEW")
    local_copy_policy: Mapped[str] = mapped_column(String(20), server_default="UNKNOWN")
    local_retention_days: Mapped[int | None] = mapped_column(Integer)
    expected_interval_s: Mapped[int | None] = mapped_column(Integer)
    source_timezone: Mapped[str | None] = mapped_column(String(64))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS)


class TrialSession(Base):
    """실증회차 (Session). SQLAlchemy Session과 이름이 겹쳐 TrialSession으로 둔다."""

    __tablename__ = "sessions"
    __table_args__ = (UniqueConstraint("organization_id", "label", name="org_label"),)
    session_id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.organization_id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), index=True)
    label: Mapped[str] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(200))
    starts_at: Mapped[datetime] = mapped_column(TS)
    ends_at: Mapped[datetime | None] = mapped_column(TS)
    site: Mapped[str | None] = mapped_column(String(200))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS)


class SessionDevice(Base):
    __tablename__ = "session_devices"
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.session_id"), primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"), primary_key=True)


class JobRow(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_claim", "status", "priority", "run_after"),  # 상태별 조회 (임대 회수·실패 원인·대기 수)
        # 꺼내기 순서 그대로, QUEUED만 (pg_queue.claim의 ORDER BY, ADR-0040)
        Index("ix_jobs_claim_queued", text("priority DESC"), "run_after", "created_at",
              postgresql_where=text("status = 'QUEUED'")),
        Index("uq_jobs_dedupe_key", "dedupe_key", unique=True, postgresql_where=text("dedupe_key IS NOT NULL")),
    )
    job_id: Mapped[str] = mapped_column(ID, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20))
    priority: Mapped[int] = mapped_column(Integer, server_default="0")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, server_default="5")
    run_after: Mapped[datetime] = mapped_column(TS)
    locked_by: Mapped[str | None] = mapped_column(String(120))
    locked_at: Mapped[datetime | None] = mapped_column(TS)
    last_error_code: Mapped[str | None] = mapped_column(String(60))
    organization_id: Mapped[str | None] = mapped_column(ID)
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    asset_id: Mapped[str | None] = mapped_column(ID, index=True)  # 05: 대상 자산
    resource_class: Mapped[str] = mapped_column(String(10), server_default="CPU")  # CPU / GPU
    lease_expires_at: Mapped[datetime | None] = mapped_column(TS)  # 임대 만료 (요구사항 1.3)
    created_at: Mapped[datetime] = mapped_column(TS)
    updated_at: Mapped[datetime] = mapped_column(TS)


class JobPolicy(Base):
    """작업 유형별 정책 (05번 요구사항 6, HW-3: 수치는 표에서)."""

    __tablename__ = "job_policies"
    job_type: Mapped[str] = mapped_column(String(60), primary_key=True)
    priority: Mapped[int] = mapped_column(Integer)
    resource_class: Mapped[str] = mapped_column(String(10), server_default="CPU")
    gpu_fallback: Mapped[str] = mapped_column(String(10), server_default="CPU")  # GPU 없을 때 CPU / WAIT
    max_attempts: Mapped[int] = mapped_column(Integer, server_default="5")
    lease_s: Mapped[int] = mapped_column(Integer, server_default="600")


# ---- 02 인증·테넌트·감사


class EnrollmentCode(Base):
    __tablename__ = "enrollment_codes"
    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"), index=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.organization_id"))
    expires_at: Mapped[datetime] = mapped_column(TS)
    used_at: Mapped[datetime | None] = mapped_column(TS)
    created_by: Mapped[str] = mapped_column(ID)
    created_at: Mapped[datetime] = mapped_column(TS)


class DeviceCredential(Base):
    __tablename__ = "device_credentials"
    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"), index=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.organization_id"))
    secret_hash: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), server_default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(TS)
    revoked_at: Mapped[datetime | None] = mapped_column(TS)


class ApiKey(Base):
    __tablename__ = "api_keys"
    api_key_id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.organization_id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    secret_hash: Mapped[str] = mapped_column(String(200))
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(40)))
    expires_at: Mapped[datetime] = mapped_column(TS)
    revoked_at: Mapped[datetime | None] = mapped_column(TS)
    created_by: Mapped[str] = mapped_column(ID)
    created_at: Mapped[datetime] = mapped_column(TS)


class AuditLog(Base):
    """추가 전용 감사 기록 (security.md). UPDATE·DELETE는 트리거와 권한으로 막는다."""

    __tablename__ = "audit_log"
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    audit_id: Mapped[str] = mapped_column(ID, unique=True)
    occurred_at: Mapped[datetime] = mapped_column(TS)
    actor_kind: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[str | None] = mapped_column(String(64))
    actor_organization_id: Mapped[str | None] = mapped_column(ID, index=True)
    organization_id: Mapped[str | None] = mapped_column(ID, index=True)  # 대상 데이터의 기관
    action: Mapped[str] = mapped_column(String(60))
    target_type: Mapped[str | None] = mapped_column(String(40))
    target_id: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[str] = mapped_column(String(20))
    source_ip: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(26))
    reason: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    prev_hash: Mapped[str] = mapped_column(String(64))
    row_hash: Mapped[str] = mapped_column(String(64))


class BreakGlassGrant(Base):
    __tablename__ = "break_glass_grants"
    grant_id: Mapped[str] = mapped_column(ID, primary_key=True)  # brk_
    requested_by: Mapped[str] = mapped_column(ID)
    approved_by: Mapped[str | None] = mapped_column(ID)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.organization_id"))
    target_type: Mapped[str] = mapped_column(String(40))
    target_id: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    duration_s: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))  # REQUESTED / ACTIVE / EXPIRED / DENIED
    starts_at: Mapped[datetime | None] = mapped_column(TS)
    expires_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS)


class IdempotencyRecord(Base):
    """Idempotency-Key 보관 (domain.md 7장). 범위 = (기관, 자격증명 주체, 메서드+경로)."""

    __tablename__ = "idempotency_keys"
    organization_id: Mapped[str] = mapped_column(ID, primary_key=True)
    principal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    method_path: Mapped[str] = mapped_column(String(300), primary_key=True)
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    body_hash: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TS)
    expires_at: Mapped[datetime] = mapped_column(TS, index=True)




# ---- 03 메타데이터 표준


class Asset(Base):
    """공통 메타데이터 v1.0 (domain.md 8장). 확장(video_ext 등)은 ext JSONB에 둔다."""

    __tablename__ = "assets"
    __table_args__ = (
        # 멱등 키는 삭제되지 않은 자산에만 (삭제 후 같은 파일을 다시 받을 수 있게, 08번)
        Index("uq_assets_idem_live", "organization_id", "source_system_id", "checksum_sha256", unique=True,
              postgresql_where=text("deleted_at IS NULL")),
        Index("ix_assets_org_status", "organization_id", "ingest_status"),
        # READY_PRIVATE가 아닌 상태 목록을 커서 순서(asset_id)대로 (대시보드 처리 대기·격리, ADR-0040)
        Index("ix_assets_not_ready", "organization_id", "ingest_status", "asset_id",
              postgresql_where=text("ingest_status <> 'READY_PRIVATE'")),
        Index("ix_assets_location", "location", postgresql_using="gist"),
        Index("ix_assets_capture", "organization_id", "capture_start_at"),
        # 계층 이동 후보 찾기 (08번 요구사항 9.2): 계층·존별로 오래된 것부터
        Index("ix_assets_tier_age", "storage_tier", "storage_zone", "ingested_at",
              postgresql_where=text("deleted_at IS NULL")),
        # 최근 들어온 자산 (11번 감시 규칙·실패 원인, ADR-0040)
        Index("ix_assets_ingested_at", "ingested_at"),
    )
    asset_id: Mapped[str] = mapped_column(ID, primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(20))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.organization_id"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.session_id"), index=True)
    source_system_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"), index=True)
    dataset_id: Mapped[str | None] = mapped_column(ID)
    data_type: Mapped[str] = mapped_column(String(20))
    variant: Mapped[str] = mapped_column(String(20))
    data_role: Mapped[str] = mapped_column(String(20))
    capture_start_at: Mapped[datetime | None] = mapped_column(TS)
    capture_end_at: Mapped[datetime | None] = mapped_column(TS)
    ingested_at: Mapped[datetime] = mapped_column(TS)
    timestamp_quality: Mapped[dict[str, Any]] = mapped_column(JSONB)
    location: Mapped[dict[str, Any] | None] = mapped_column(Geometry())
    source_location: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    area_code: Mapped[str | None] = mapped_column(String(40))
    location_precision: Mapped[str | None] = mapped_column(String(20))
    mime_type: Mapped[str] = mapped_column(String(120))
    byte_size: Mapped[int] = mapped_column(BigInteger)
    checksum_sha256: Mapped[str] = mapped_column(String(64))
    has_binary: Mapped[bool] = mapped_column(Boolean)
    collection_mode: Mapped[str] = mapped_column(String(30))
    privacy_status: Mapped[str] = mapped_column(String(30))
    sensitivity_class: Mapped[str] = mapped_column(String(20))
    sharing_scope: Mapped[str] = mapped_column(String(30))
    legal_basis_ref: Mapped[str | None] = mapped_column(String(200))
    retention_policy_id: Mapped[str] = mapped_column(String(60))
    retention_until: Mapped[datetime] = mapped_column(TS)
    quality_status: Mapped[str] = mapped_column(String(20))
    quality_reasons: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    ingest_status: Mapped[str] = mapped_column(String(20))
    quarantine_reason: Mapped[str | None] = mapped_column(String(40))
    share_status: Mapped[str] = mapped_column(String(20))
    review_result: Mapped[str | None] = mapped_column(String(10))
    backup_class: Mapped[str] = mapped_column(String(20))
    # 기관이 신청한 백업 (domain.md 9장 2번). 이 자산의 용량은 org_quotas.backup_bytes에 들어 있다 (08번 요구사항 6.4)
    backup_requested_at: Mapped[datetime | None] = mapped_column(TS)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB)
    env_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    tags: Mapped[list[Any] | None] = mapped_column(JSONB)
    ext: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    ingest_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # 내부용(감시 규칙·로컬 ID·좌표 변환 이력)
    storage_zone: Mapped[str | None] = mapped_column(String(20))
    storage_key: Mapped[str | None] = mapped_column(String(400))
    # 08: 객체가 있는 계층 HOT / WARM (요구사항 9). 키는 계층과 무관하게 같다. 이동은 sha256 확인 뒤에만 바꾼다
    storage_tier: Mapped[str] = mapped_column(String(10), server_default="HOT", default="HOT")
    last_accessed_at: Mapped[datetime | None] = mapped_column(TS)  # 08: 사용자가 바이너리를 마지막으로 연 시각
    embedding: Mapped[Any | None] = mapped_column(Vector(), deferred=True)
    legal_hold: Mapped[bool] = mapped_column(Boolean, server_default="false")  # 08: 법적 보류
    legal_hold_reason: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(TS, index=True)  # 08: 삭제 표시 (기록은 남긴다)
    deletion_certificate_id: Mapped[str | None] = mapped_column(ID)
    last_reprocessed_at: Mapped[datetime | None] = mapped_column(TS)  # 10: 근거 손상 감지(재처리)
    created_at: Mapped[datetime] = mapped_column(TS)
    updated_at: Mapped[datetime] = mapped_column(TS)


class AssetLineage(Base):
    """계보 (03번 요구사항 11). 파생 자산 → 원 자산."""

    __tablename__ = "asset_lineage"
    child_asset_id: Mapped[str] = mapped_column(ForeignKey("assets.asset_id"), primary_key=True)
    parent_asset_id: Mapped[str] = mapped_column(ForeignKey("assets.asset_id"), primary_key=True, index=True)
    relation: Mapped[str] = mapped_column(String(20))
    tool_version: Mapped[str | None] = mapped_column(String(120))
    model_ref: Mapped[str | None] = mapped_column(String(200))
    run_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS)


class DevicePolicy(Base):
    """원천 장비·수집기 정책 버전 (03번 요구사항 1.3). 내용은 contracts/schemas/device-policy.schema.json."""

    __tablename__ = "device_policies"
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.organization_id"), index=True)
    policy: Mapped[dict[str, Any]] = mapped_column(JSONB)
    integration_card_id: Mapped[str | None] = mapped_column(ID)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS)


class Agreement(Base):
    """기관 약정 (domain.md 1장, 기준 문서 14.1장). TIER1=수집·관리(L0·L1), TIER2=공동활용 제공(L2~L4)."""

    __tablename__ = "agreements"
    agreement_id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.organization_id"), index=True)
    tier: Mapped[str] = mapped_column(String(10))
    reference: Mapped[str] = mapped_column(String(200))
    valid_from: Mapped[datetime] = mapped_column(TS)
    valid_until: Mapped[datetime | None] = mapped_column(TS)
    allowed_purposes: Mapped[list[str]] = mapped_column(ARRAY(String(40)), server_default="{}")
    status: Mapped[str] = mapped_column(String(20), server_default="ACTIVE")
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS)


class IntegrationCard(Base):
    """연계 카드 (ONB-01). 기관·장비 단위, 버전 관리. 화면은 09번."""

    __tablename__ = "integration_cards"
    integration_card_id: Mapped[str] = mapped_column(ID, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.organization_id"), index=True)
    device_id: Mapped[str | None] = mapped_column(ForeignKey("devices.device_id"))
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS)


class PolicyDraft(Base):
    """장비 정책 초안 (09번 요구사항 3.2·3.3, ADR-0037). DRAFT → APPLIED | REJECTED.

    운영자가 적용하기 전까지 장비 기록·장비 정책을 바꾸지 않는다. 장비마다 DRAFT는 하나뿐이다.
    fields: 항목별 {status: FROM_CARD | ENTERED | NEEDS_CONFIRMATION, value?, card_pointer?} (domain/onboarding.py)."""

    __tablename__ = "policy_drafts"
    __table_args__ = (
        Index("uq_policy_drafts_open_device", "device_id", unique=True, postgresql_where=text("status = 'DRAFT'")),
    )
    policy_draft_id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.organization_id"), index=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"), index=True)
    collector_device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"))
    integration_card_id: Mapped[str | None] = mapped_column(ID)
    integration_card_version: Mapped[int | None] = mapped_column(Integer)
    fields: Mapped[dict[str, Any]] = mapped_column(JSONB)
    note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), server_default="DRAFT")
    revision: Mapped[int] = mapped_column(Integer, server_default="1")
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS)
    updated_by: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(TS)
    decided_by: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime | None] = mapped_column(TS)
    decision_reason: Mapped[str | None] = mapped_column(String(500))
    applied: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # 저장한 정책 버전·바뀐 값 (APPLIED일 때)


class OrgEventCode(Base):
    """기관별 하위 이벤트 코드 (03번 요구사항 8.3). 공통 상위분류는 contracts/codes/event-categories.json."""

    __tablename__ = "org_event_codes"
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.organization_id"), primary_key=True)
    code: Mapped[str] = mapped_column(String(80), primary_key=True)  # 상위분류.하위코드
    label: Mapped[str] = mapped_column(String(120))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS)


class QualityRuleSet(Base):
    __tablename__ = "quality_rule_sets"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    rules: Mapped[dict[str, Any]] = mapped_column(JSONB)
    active: Mapped[bool] = mapped_column(Boolean, server_default="false")
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS)


# ---- 04 수집


class Upload(Base):
    """업로드 세션 (전송 작업 단위, 실증회차와 다르다)."""

    __tablename__ = "uploads"
    __table_args__ = (
        # 만료 정리는 진행 중인 업로드만 본다 / 최근 생성분 실패율 / 최근 실패 원인 (ADR-0040)
        Index("ix_uploads_uploading_expires", "expires_at", postgresql_where=text("status = 'UPLOADING'")),
        Index("ix_uploads_created_at", "created_at"),
        Index("ix_uploads_failed_updated", "updated_at", postgresql_where=text("error_code IS NOT NULL")),
    )
    upload_id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.organization_id"), index=True)
    # 받은 것 없이 취소·만료된 새 자산 기록은 지우고 업로드 기록만 남긴다
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.asset_id", ondelete="SET NULL"), index=True)
    source_system_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"))
    created_by: Mapped[str] = mapped_column(String(64))
    bucket: Mapped[str] = mapped_column(String(120))
    object_key: Mapped[str] = mapped_column(String(400))
    s3_upload_id: Mapped[str] = mapped_column(String(400))
    part_size: Mapped[int] = mapped_column(BigInteger)
    part_count: Mapped[int] = mapped_column(Integer)
    declared_size: Mapped[int] = mapped_column(BigInteger)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20))  # UPLOADING / RECEIVED / ABORTED / EXPIRED
    asset_was_metadata_only: Mapped[bool] = mapped_column(Boolean, server_default="false")
    error_code: Mapped[str | None] = mapped_column(String(60))
    expires_at: Mapped[datetime] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS)
    updated_at: Mapped[datetime] = mapped_column(TS)


class OrgQuota(Base):
    """기관 사용량 (domain.md 9장): 사용량 = 등록 객체 + 격리 + 진행 중 예약 + 신청 백업."""

    __tablename__ = "org_quotas"
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.organization_id"), primary_key=True)
    quota_bytes: Mapped[int] = mapped_column(BigInteger)
    used_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0")
    quarantine_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0")
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0")
    backup_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0")
    last_alert_pct: Mapped[int] = mapped_column(Integer, server_default="0")  # 08: 80·90·100% 알림 중복 방지
    updated_at: Mapped[datetime] = mapped_column(TS)


class IntegrityLogEntry(Base):
    """무결성 로그 (04번 요구사항 4.6). 추가 전용 해시 체인."""

    __tablename__ = "integrity_log"
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.asset_id"), unique=True)
    checksum_sha256: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column(BigInteger)
    registered_at: Mapped[datetime] = mapped_column(TS)
    prev_hash: Mapped[str] = mapped_column(String(64))
    entry_hash: Mapped[str] = mapped_column(String(64))


class AgentStatus(Base):
    """수집기 최근 하트비트 (04번 요구사항 11.2). 이력·지연 경보는 11번."""

    __tablename__ = "agent_status"
    collector_device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.organization_id"), index=True)
    last_heartbeat_at: Mapped[datetime] = mapped_column(TS)
    agent_version: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(String(64))
    last_success_at: Mapped[datetime | None] = mapped_column(TS)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    queue_items: Mapped[int] = mapped_column(Integer)
    queue_bytes: Mapped[int] = mapped_column(BigInteger)
    heartbeat: Mapped[dict[str, Any]] = mapped_column(JSONB)


# ---- 05 처리 파이프라인


class IntegrityAnchor(Base):
    """무결성 로그 일 단위 앵커 (05번 요구사항 5.2)."""

    __tablename__ = "integrity_anchors"
    day: Mapped[datetime] = mapped_column(DateTime(timezone=False), primary_key=True)
    last_seq: Mapped[int] = mapped_column(BigInteger)
    entry_count: Mapped[int] = mapped_column(Integer)
    anchor_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS)


class ModelRegistryEntry(Base):
    """Model_Registry (05번 요구사항 8.3): 이름·버전·sha256·라이선스가 기록된 모델만 로드한다."""

    __tablename__ = "model_registry"
    name: Mapped[str] = mapped_column(String(120), primary_key=True)
    version: Mapped[str] = mapped_column(String(60), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64))
    license: Mapped[str | None] = mapped_column(String(60))
    weights_uri: Mapped[str] = mapped_column(String(400))
    training_data_terms: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS)


# ---- 07 센서·이벤트


class ObservationMapping(Base):
    """원천 장비별 관측값 매핑 프로필 버전 (07번 요구사항 4)."""

    __tablename__ = "observation_mappings"
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.organization_id"), index=True)
    mapping: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS)


class ObservationCorrection(Base):
    """관측값 정정 이력 (07번 요구사항 3.2). 이전 값을 보존한다."""

    __tablename__ = "observation_corrections"
    correction_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[str] = mapped_column(ID, index=True)
    source_system_id: Mapped[str] = mapped_column(ID)
    metric: Mapped[str] = mapped_column(String(80))
    observed_at: Mapped[datetime] = mapped_column(TS)
    depth_m: Mapped[float | None] = mapped_column(Float)
    channel: Mapped[str | None] = mapped_column(String(32))
    old_value: Mapped[float | None] = mapped_column(Float)
    old_quality_flag: Mapped[str] = mapped_column(String(20))
    new_value: Mapped[float | None] = mapped_column(Float)
    new_quality_flag: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text)
    corrected_by: Mapped[str] = mapped_column(String(64))
    corrected_at: Mapped[datetime] = mapped_column(TS)


class Event(Base):
    """실증 이벤트 (07번 요구사항 7)."""

    __tablename__ = "events"
    __table_args__ = (
        Index("uq_events_source_event", "organization_id", "source_system_id", "source_event_id", unique=True,
              postgresql_where=text("source_event_id IS NOT NULL")),
        Index("ix_events_session_time", "organization_id", "session_id", "started_at"),
    )
    event_id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.organization_id"))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.session_id"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"))
    source_system_id: Mapped[str | None] = mapped_column(ForeignKey("devices.device_id"))
    event_code: Mapped[str] = mapped_column(String(80))
    started_at: Mapped[datetime] = mapped_column(TS)
    ended_at: Mapped[datetime | None] = mapped_column(TS)
    timestamp_quality: Mapped[dict[str, Any]] = mapped_column(JSONB)
    location: Mapped[dict[str, Any] | None] = mapped_column(Geometry())
    severity: Mapped[str | None] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(10))
    model_ref: Mapped[str | None] = mapped_column(String(128))
    confidence: Mapped[float | None] = mapped_column(Float)
    human_verified: Mapped[bool] = mapped_column(Boolean, server_default="false")
    verified_by: Mapped[str | None] = mapped_column(ID)
    verified_at: Mapped[datetime | None] = mapped_column(TS)
    source_event_id: Mapped[str | None] = mapped_column(String(128))
    note: Mapped[str | None] = mapped_column(Text)
    schema_version: Mapped[str] = mapped_column(String(20))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS)


class EventAsset(Base):
    __tablename__ = "event_assets"
    event_id: Mapped[str] = mapped_column(ForeignKey("events.event_id"), primary_key=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.asset_id"), primary_key=True)


class SensorParseRun(Base):
    """센서 파일 파싱 결과 (07번 요구사항 5.3, 5.4). 행 번호별 사유와 이전 결과와의 차이."""

    __tablename__ = "sensor_parse_runs"
    run_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.asset_id"), index=True)
    organization_id: Mapped[str] = mapped_column(ID)
    mapping_version: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(10))  # OK / PARTIAL / FAILED
    accepted: Mapped[int] = mapped_column(Integer, server_default="0")
    duplicates: Mapped[int] = mapped_column(Integer, server_default="0")
    rejected: Mapped[int] = mapped_column(Integer, server_default="0")
    row_errors: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    diff_from_previous: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TS)


# ---- 08 저장·보존


class RetentionPolicy(Base):
    """대상별 보존 기간·만료 동작 (08번 요구사항 3.1). 대상: RAW·PROXY·…·QUARANTINE:<사유>. days가 NULL이면 무기한."""

    __tablename__ = "retention_policies"
    target: Mapped[str] = mapped_column(String(60), primary_key=True)
    policy_id: Mapped[str] = mapped_column(String(64), unique=True)
    retention_days: Mapped[int | None] = mapped_column(Integer)
    expiry_action: Mapped[str] = mapped_column(String(10))  # DELETE / KEEP
    notify_days_before: Mapped[int] = mapped_column(Integer, server_default="30")
    updated_at: Mapped[datetime] = mapped_column(TS)


class DeletionCertificate(Base):
    """삭제 증명 (08번 요구사항 5.1, 5.2)."""

    __tablename__ = "deletion_certificates"
    certificate_id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(ID, index=True)
    scope: Mapped[str] = mapped_column(String(10))  # ASSET / SESSION / RETENTION
    target_id: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(String(64))
    deleted_at: Mapped[datetime] = mapped_column(TS)
    objects: Mapped[list[Any]] = mapped_column(JSONB)
    backup_copies_expire_at: Mapped[datetime | None] = mapped_column(TS)


class TierPolicy(Base):
    """Hot → Warm 이동 규칙 (08번 요구사항 9.2, HW-3). 값은 정책이다 — 운영기관 확정 필요."""

    __tablename__ = "tier_policies"
    policy_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    zone: Mapped[str] = mapped_column(String(20))  # RAW / PROCESSED / EVIDENCE / DATASET (격리는 옮기지 않는다)
    variant: Mapped[str | None] = mapped_column(String(20))  # NULL이면 그 존의 모든 변형
    min_age_days: Mapped[int] = mapped_column(Integer)  # 들어온 뒤(ingested_at) 지난 일수
    # 마지막 조회(없으면 ingested_at) 뒤 일수, NULL이면 보지 않는다
    min_idle_days: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default="false")
    updated_at: Mapped[datetime] = mapped_column(TS)


class TierMove(Base):
    """계층 이동 기록 (08번 요구사항 9.2·9.3). 자산마다 열린 이동(COPYING·SWITCHED)은 하나뿐이다.

    COPYING(원래 위치로 계속 읽음) → 사본 sha256 확인 → SWITCHED(자산 위치를 바꿈, 원래 객체는 유예 동안 남김)
    → DONE(원래 객체 삭제). 실패하면 FAILED, 자산이 그사이 바뀌거나 지워지면 CANCELLED — 둘 다 원래 객체는 그대로다.
    """

    __tablename__ = "tier_moves"
    __table_args__ = (
        Index("uq_tier_moves_open", "asset_id", unique=True,
              postgresql_where=text("status IN ('COPYING', 'SWITCHED')")),
        Index("ix_tier_moves_status_due", "status", "delete_after"),
    )
    move_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.asset_id"), index=True)
    organization_id: Mapped[str] = mapped_column(ID)
    policy_id: Mapped[str | None] = mapped_column(String(64))  # NULL이면 운영자 되돌리기(RECALL)
    from_tier: Mapped[str] = mapped_column(String(10))
    to_tier: Mapped[str] = mapped_column(String(10))
    zone: Mapped[str] = mapped_column(String(20))
    object_key: Mapped[str] = mapped_column(String(400))
    byte_size: Mapped[int] = mapped_column(BigInteger)
    checksum_sha256: Mapped[str] = mapped_column(String(64))  # 자산 기록의 값 — 사본을 다시 읽어 이것과 비교한다
    status: Mapped[str] = mapped_column(String(12))  # COPYING / SWITCHED / DONE / FAILED / CANCELLED
    error_code: Mapped[str | None] = mapped_column(String(60))
    legal_hold: Mapped[bool | None] = mapped_column(Boolean)  # 위치를 바꿀 때 법적 보류였는지 (보관 경위 기록)
    created_at: Mapped[datetime] = mapped_column(TS)
    updated_at: Mapped[datetime] = mapped_column(TS)
    switched_at: Mapped[datetime | None] = mapped_column(TS)
    delete_after: Mapped[datetime | None] = mapped_column(TS)  # 원래 객체를 지워도 되는 시각 (≥ 다운로드 URL 유효시간)
    finished_at: Mapped[datetime | None] = mapped_column(TS)


class DailyTask(Base):
    """하루 한 번 도는 정비 작업의 마지막 실행일 (보존·대조·예측)."""

    __tablename__ = "daily_tasks"
    name: Mapped[str] = mapped_column(String(60), primary_key=True)
    last_run_on: Mapped[datetime] = mapped_column(DateTime(timezone=False))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


# ---- 11 수집 모니터링


class SourceActivity(Base):
    """장비별 마지막 수신 (11번 요구사항 1). 등록·관측 저장 때 갱신해 큰 표를 훑지 않는다."""

    __tablename__ = "source_activity"
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ID, index=True)
    last_registered_at: Mapped[datetime | None] = mapped_column(TS)
    last_observation_at: Mapped[datetime | None] = mapped_column(TS)
    last_observation_received_at: Mapped[datetime | None] = mapped_column(TS)
    last_clock_offset_ms: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(TS)


class WatchRule(Base):
    """감시 규칙 정책 표 (11번 요구사항 2). 임계값은 params에, 코드 수정 없이 조정한다."""

    __tablename__ = "watch_rules"
    rule_code: Mapped[str] = mapped_column(String(40), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean)
    severity: Mapped[str] = mapped_column(String(10))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    notify_org: Mapped[bool] = mapped_column(Boolean)
    notify_operator: Mapped[bool] = mapped_column(Boolean)
    channels: Mapped[list[str]] = mapped_column(JSONB)
    realert_interval_s: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(TS)
    updated_by: Mapped[str | None] = mapped_column(String(64))


class WatchAlert(Base):
    """규칙 위반 상태. 규칙+대상 하나당 한 행 (alert_key). 위반이 이어지는 동안 active."""

    __tablename__ = "watch_alerts"
    alert_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    rule_code: Mapped[str] = mapped_column(String(40))
    organization_id: Mapped[str | None] = mapped_column(ID, index=True)
    device_id: Mapped[str | None] = mapped_column(ID)
    session_id: Mapped[str | None] = mapped_column(ID)
    active: Mapped[bool] = mapped_column(Boolean)
    first_seen_at: Mapped[datetime] = mapped_column(TS)
    last_seen_at: Mapped[datetime] = mapped_column(TS)
    last_notified_at: Mapped[datetime | None] = mapped_column(TS)
    cleared_at: Mapped[datetime | None] = mapped_column(TS)
    incident_id: Mapped[str | None] = mapped_column(ID)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB)


class Incident(Base):
    """경보에서 만든 장애 기록 (11번 요구사항 4)."""

    __tablename__ = "incidents"
    __table_args__ = (Index("ix_incidents_org_opened", "organization_id", "opened_at"),)
    incident_id: Mapped[str] = mapped_column(ID, primary_key=True)
    alert_key: Mapped[str] = mapped_column(String(200), index=True)
    rule_code: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20))  # OPEN / ACKNOWLEDGED / RESOLVED
    organization_id: Mapped[str | None] = mapped_column(ID)
    device_id: Mapped[str | None] = mapped_column(ID)
    session_id: Mapped[str | None] = mapped_column(ID)
    summary: Mapped[str] = mapped_column(Text)
    guidance: Mapped[str] = mapped_column(Text)
    opened_at: Mapped[datetime] = mapped_column(TS)
    acknowledged_at: Mapped[datetime | None] = mapped_column(TS)
    resolved_at: Mapped[datetime | None] = mapped_column(TS)
    resolved_by: Mapped[str | None] = mapped_column(String(64))
    assignee: Mapped[str | None] = mapped_column(String(120))
    memo: Mapped[str | None] = mapped_column(Text)


class HardwareSignalState(Base):
    """하드웨어 신호의 최신 상태 (11번 요구사항 7). 실제 수집 어댑터는 하드웨어 확정 후."""

    __tablename__ = "hardware_signals"
    host: Mapped[str] = mapped_column(String(120), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    component: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(10))
    detail: Mapped[str | None] = mapped_column(String(500))
    observed_at: Mapped[datetime] = mapped_column(TS)
    received_at: Mapped[datetime] = mapped_column(TS)


class Inspection(Base):
    """정기 점검 수행 기록 (11번 요구사항 6)."""

    __tablename__ = "inspections"
    __table_args__ = (UniqueConstraint("cycle", "period_key", name="cycle_period"),)
    inspection_id: Mapped[str] = mapped_column(ID, primary_key=True)
    cycle: Mapped[str] = mapped_column(String(10))
    period_key: Mapped[str] = mapped_column(String(12))
    status: Mapped[str] = mapped_column(String(12))
    assignee: Mapped[str | None] = mapped_column(String(120))
    due_at: Mapped[datetime] = mapped_column(TS)
    note: Mapped[str | None] = mapped_column(Text)
    items: Mapped[list[Any]] = mapped_column(JSONB)
    evidence: Mapped[list[Any]] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS)
    completed_at: Mapped[datetime | None] = mapped_column(TS)
    completed_by: Mapped[str | None] = mapped_column(String(64))


# ---- 10 실증 기록·증빙·보고서


class SessionRecord(Base):
    """회차 기록 (10번 요구사항 1). 내용은 버전 표에, 수정은 항상 새 버전."""

    __tablename__ = "session_records"
    record_id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.organization_id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.session_id"), index=True)
    latest_version: Mapped[int] = mapped_column(Integer)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS)
    updated_at: Mapped[datetime] = mapped_column(TS)


class SessionRecordVersion(Base):
    __tablename__ = "session_record_versions"
    record_id: Mapped[str] = mapped_column(ForeignKey("session_records.record_id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))
    title: Mapped[str] = mapped_column(String(200))
    test_item: Mapped[str | None] = mapped_column(String(200))
    conditions: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    evidence_refs: Mapped[list[Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(10))  # DRAFT / APPROVED
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(TS)
    evidence_damaged: Mapped[bool] = mapped_column(Boolean, server_default="false")
    damage: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS)


class EvidenceCitation(Base):
    """증빙·보고서 버전이 인용한 자산 역색인 (근거 손상 감지, 08번 삭제 영향)."""

    __tablename__ = "evidence_citations"
    __table_args__ = (UniqueConstraint("citing_type", "citing_id", "citing_version", "asset_id", name="citation"),)
    citation_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[str] = mapped_column(ID)
    citing_type: Mapped[str] = mapped_column(String(10))  # RECORD / REPORT
    citing_id: Mapped[str] = mapped_column(ID)
    citing_version: Mapped[int] = mapped_column(Integer)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.asset_id"), index=True)
    cited_at: Mapped[datetime] = mapped_column(TS)
    damage_reason: Mapped[str | None] = mapped_column(String(20))  # 감지되면 DELETED / QUARANTINED / REPROCESSED


class MetricDefinition(Base):
    """지표 정의 (10번 요구사항 3.1). 코드별 버전."""

    __tablename__ = "metric_definitions"
    code: Mapped[str] = mapped_column(String(60), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    unit: Mapped[str | None] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(30))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS)


class ReportTemplate(Base):
    """보고서 양식 (10번 요구사항 4.6). 실제 관리기관 양식은 확보 후 등록."""

    __tablename__ = "report_templates"
    code: Mapped[str] = mapped_column(String(60), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    scope: Mapped[str] = mapped_column(String(10))  # SESSION / PERIOD
    sections: Mapped[list[Any]] = mapped_column(JSONB)
    hwpx_key: Mapped[str | None] = mapped_column(String(400))
    hwpx_sha256: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS)


class Report(Base):
    __tablename__ = "reports"
    report_id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.organization_id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.session_id"))
    period_from: Mapped[datetime | None] = mapped_column(TS)
    period_to: Mapped[datetime | None] = mapped_column(TS)
    title: Mapped[str] = mapped_column(String(200))
    template_code: Mapped[str] = mapped_column(String(60))
    template_version: Mapped[int] = mapped_column(Integer)
    latest_version: Mapped[int] = mapped_column(Integer)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS)


class ReportVersion(Base):
    """보고서 버전 (report_status는 domain.md 5장). 승인본은 EVIDENCE 존 스냅숏과 sha256으로 고정."""

    __tablename__ = "report_versions"
    report_id: Mapped[str] = mapped_column(ForeignKey("reports.report_id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(12))
    generated_at: Mapped[datetime] = mapped_column(TS)
    data_as_of: Mapped[datetime] = mapped_column(TS)
    variables: Mapped[dict[str, Any]] = mapped_column(JSONB)
    fields: Mapped[list[Any]] = mapped_column(JSONB)
    metrics: Mapped[list[Any]] = mapped_column(JSONB)
    record_refs: Mapped[list[Any]] = mapped_column(JSONB)
    evidence_damaged: Mapped[bool] = mapped_column(Boolean, server_default="false")
    damage: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(TS)
    snapshot_key: Mapped[str | None] = mapped_column(String(400))
    snapshot_sha256: Mapped[str | None] = mapped_column(String(64))
    submitted_by: Mapped[str | None] = mapped_column(String(64))
    submitted_at: Mapped[datetime | None] = mapped_column(TS)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS)


# ---- 13 공유 등급·데이터셋·배포


class ShareRequest(Base):
    """공유 등급 변경 신청 (13번 요구사항 3, 10). 상향은 Sharing_Rules, 하향은 언제든."""

    __tablename__ = "share_requests"
    share_request_id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(ID, index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.asset_id"), index=True)
    from_scope: Mapped[str] = mapped_column(String(30))
    target_scope: Mapped[str] = mapped_column(String(30))
    state: Mapped[str] = mapped_column(String(12))  # APPLIED / IN_REVIEW / DENIED
    reason: Mapped[str] = mapped_column(Text)
    existing_grant_handling: Mapped[str | None] = mapped_column(String(20))
    requested_by: Mapped[str] = mapped_column(String(64))
    requested_at: Mapped[datetime] = mapped_column(TS)
    decided_by: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime | None] = mapped_column(TS)
    decision_reason: Mapped[str | None] = mapped_column(Text)


class ShareReviewEvent(Base):
    """공유 심사 이력 (원본 단위 share_status 전이, 13번 요구사항 4)."""

    __tablename__ = "share_review_events"
    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.asset_id"), index=True)
    organization_id: Mapped[str] = mapped_column(ID)
    from_status: Mapped[str] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20))
    actor: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text)
    legal_basis_ref: Mapped[str | None] = mapped_column(String(200))
    at: Mapped[datetime] = mapped_column(TS)


class Dataset(Base):
    __tablename__ = "datasets"
    dataset_id: Mapped[str] = mapped_column(ID, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    published: Mapped[bool] = mapped_column(Boolean, server_default="false")
    published_at: Mapped[datetime | None] = mapped_column(TS)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS)


class DatasetVersion(Base):
    """데이터셋 버전 (13번 요구사항 5). 만든 뒤 바꾸지 않는다 — 변경은 새 버전."""

    __tablename__ = "dataset_versions"
    __table_args__ = (UniqueConstraint("dataset_id", "version", name="dataset_version"),)
    dataset_version_id: Mapped[str] = mapped_column(ID, primary_key=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.dataset_id"), index=True)
    version: Mapped[str] = mapped_column(String(20))
    provider_organization_ids: Mapped[list[str]] = mapped_column(ARRAY(String(30)))
    item_count: Mapped[int] = mapped_column(Integer)
    total_bytes: Mapped[int] = mapped_column(BigInteger)
    data_card: Mapped[dict[str, Any]] = mapped_column(JSONB)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB)
    manifest_key: Mapped[str] = mapped_column(String(400))
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS)
    # 큰 버전은 작업 큐에서 만든다 (13번 작업 32): BUILDING → READY | FAILED. 이용기관·이용 신청은 READY만
    status: Mapped[str] = mapped_column(String(12), server_default="READY")
    build_error: Mapped[str | None] = mapped_column(String(500))
    pending_asset_ids: Mapped[list[str] | None] = mapped_column(ARRAY(String(30)))
    # 카탈로그 샘플 썸네일 객체 키 (DATASET 버킷, 13번 요구사항 7.1). 항목의 공유용 파생본에서 뽑는다
    sample_keys: Mapped[list[str] | None] = mapped_column(ARRAY(String(400)))


class DatasetVersionItem(Base):
    __tablename__ = "dataset_version_items"
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.dataset_version_id"),
                                                    primary_key=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.asset_id"), primary_key=True, index=True)
    organization_id: Mapped[str] = mapped_column(ID)
    path: Mapped[str] = mapped_column(String(512))
    object_key: Mapped[str] = mapped_column(String(400))
    byte_size: Mapped[int] = mapped_column(BigInteger)
    checksum_sha256: Mapped[str] = mapped_column(String(64))


class AccessRequest(Base):
    """이용 신청 (13번 요구사항 8). status는 grant_status 표 (domain.md 5장)."""

    __tablename__ = "access_requests"
    access_request_id: Mapped[str] = mapped_column(ID, primary_key=True)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.dataset_version_id"), index=True)
    organization_id: Mapped[str] = mapped_column(ID, index=True)  # 신청 기관 (CONSUMER)
    requested_by: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(40))
    purpose_detail: Mapped[str] = mapped_column(Text)
    period_start: Mapped[datetime] = mapped_column(TS)
    period_end: Mapped[datetime] = mapped_column(TS)
    security_measures: Mapped[str] = mapped_column(Text)
    agreement_version: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20))
    deadline_at: Mapped[datetime] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS)
    updated_at: Mapped[datetime] = mapped_column(TS)


class AccessDecision(Base):
    """기관별 결정 (제공기관 ORG_ADMIN, 운영자). 기관마다 한 번."""

    __tablename__ = "access_decisions"
    access_request_id: Mapped[str] = mapped_column(ForeignKey("access_requests.access_request_id"), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ID, primary_key=True)
    role: Mapped[str] = mapped_column(String(20))  # PROVIDER / OPERATOR
    decision: Mapped[str] = mapped_column(String(10))
    reason: Mapped[str] = mapped_column(Text)
    decided_by: Mapped[str] = mapped_column(String(64))
    decided_at: Mapped[datetime] = mapped_column(TS)


class Grant(Base):
    """기한부 이용 권한 (13번 요구사항 8.4, 10). 데이터셋 버전 하나에만."""

    __tablename__ = "grants"
    grant_id: Mapped[str] = mapped_column(ID, primary_key=True)
    access_request_id: Mapped[str] = mapped_column(ForeignKey("access_requests.access_request_id"), unique=True)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.dataset_version_id"), index=True)
    organization_id: Mapped[str] = mapped_column(ID, index=True)  # 이용 기관
    status: Mapped[str] = mapped_column(String(20))  # GRANTED / EXPIRED / REVOKED
    starts_at: Mapped[datetime] = mapped_column(TS)
    expires_at: Mapped[datetime] = mapped_column(TS)
    revoked_at: Mapped[datetime | None] = mapped_column(TS)
    revoked_by: Mapped[str | None] = mapped_column(String(64))
    revoke_reason: Mapped[str | None] = mapped_column(Text)
    destruction_confirmed_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS)


class Distribution(Base):
    __tablename__ = "distributions"
    distribution_id: Mapped[str] = mapped_column(ID, primary_key=True)
    grant_id: Mapped[str] = mapped_column(ForeignKey("grants.grant_id"), index=True)
    organization_id: Mapped[str] = mapped_column(ID)
    requested_by: Mapped[str] = mapped_column(String(64))
    file_count: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS)


# ---- 14 비식별·검수·태깅·장면 검색


class ReviewSamplingPolicy(Base):
    """위험도별 검수 표본 비율과 태깅·임베딩 주기 (14번 요구사항 4.1, 6.1, 7.1). 값은 초안 — 운영기관이 조정."""

    __tablename__ = "review_sampling_policies"
    risk_level: Mapped[str] = mapped_column(String(10), primary_key=True)  # LOW / MEDIUM / HIGH
    sample_ratio: Mapped[float] = mapped_column(Float)
    min_samples: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(TS)


class DeidReview(Base):
    """비식별 결과 검수 배정·결정 (14번 요구사항 4, 5)."""

    __tablename__ = "deid_reviews"
    review_id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(ID, index=True)
    session_id: Mapped[str | None] = mapped_column(ID, index=True)
    original_asset_id: Mapped[str] = mapped_column(ForeignKey("assets.asset_id"))
    deid_asset_id: Mapped[str] = mapped_column(ForeignKey("assets.asset_id"), index=True)
    reviewer_id: Mapped[str] = mapped_column(ID, index=True)
    risk_level: Mapped[str] = mapped_column(String(10))
    samples: Mapped[list[Any]] = mapped_column(JSONB)
    recheck: Mapped[bool] = mapped_column(Boolean, server_default="false")
    status: Mapped[str] = mapped_column(String(16))  # ASSIGNED / PASSED / MISSED / REJECTED / SUPERSEDED
    missed: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    manual_regions: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    note: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS)


class AssetTag(Base):
    """샘플링 태그 (14번 요구사항 6). 사람이 고친 것은 source=HUMAN, 이력은 asset_tag_edits."""

    __tablename__ = "asset_tags"
    __table_args__ = (UniqueConstraint("asset_id", "tag", "t_offset", name="asset_tag"),)
    tag_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.asset_id"), index=True)
    organization_id: Mapped[str] = mapped_column(ID)
    tag: Mapped[str] = mapped_column(String(40))
    t_offset: Mapped[float] = mapped_column(Float, server_default="-1")  # 자산 전체 태그는 -1
    confidence: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(10))  # MODEL / HUMAN
    model_ref: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(TS)


class AssetTagEdit(Base):
    __tablename__ = "asset_tag_edits"
    edit_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.asset_id"), index=True)
    action: Mapped[str] = mapped_column(String(10))  # ADD / REMOVE
    tag: Mapped[str] = mapped_column(String(40))
    edited_by: Mapped[str] = mapped_column(String(64))
    edited_at: Mapped[datetime] = mapped_column(TS)


class TagEventRule(Base):
    """태그 → 실증 이벤트 규칙 정책 표 (14번 요구사항 6.4)."""

    __tablename__ = "tag_event_rules"
    tag: Mapped[str] = mapped_column(String(40), primary_key=True)
    event_code: Mapped[str] = mapped_column(String(80))
    min_confidence: Mapped[float] = mapped_column(Float)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default="true")


# ---- 12 공공 Open API 수집


class ExternalSource(Base):
    """Source_Registry (12번 요구사항 1). 인증키는 참조(환경 변수 이름)만 저장하고 값은 비밀 저장소에."""

    __tablename__ = "external_sources"
    source_id: Mapped[str] = mapped_column(String(60), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    adapter_kind: Mapped[str] = mapped_column(String(30))  # MOCK (명세 확인된 어댑터만 추가)
    spec_status: Mapped[str] = mapped_column(String(30))  # VERIFIED / NEEDS_CONFIRMATION
    endpoint: Mapped[str | None] = mapped_column(String(400))
    secret_ref: Mapped[str | None] = mapped_column(String(120))
    interval_s: Mapped[int] = mapped_column(Integer)
    max_window_s: Mapped[int] = mapped_column(Integer)
    rate_limit_per_min: Mapped[int | None] = mapped_column(Integer)
    supports_backfill: Mapped[bool] = mapped_column(Boolean)
    stations: Mapped[list[Any]] = mapped_column(JSONB)
    field_mapping: Mapped[dict[str, Any]] = mapped_column(JSONB)
    attribution: Mapped[str] = mapped_column(String(500))
    redistribution_allowed: Mapped[bool] = mapped_column(Boolean)
    options: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default="true")
    collected_until: Mapped[datetime | None] = mapped_column(TS)
    last_run_at: Mapped[datetime | None] = mapped_column(TS)
    last_success_at: Mapped[datetime | None] = mapped_column(TS)
    consecutive_failures: Mapped[int] = mapped_column(Integer, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(500))
    format_error_at: Mapped[datetime | None] = mapped_column(TS)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS)


class BackupRun(Base):
    """계층형 백업 회차 (08번 요구사항 6.3, 7.1). 대상·방법은 ADR-0021."""

    __tablename__ = "backup_runs"
    backup_run_id: Mapped[str] = mapped_column(ID, primary_key=True)
    status: Mapped[str] = mapped_column(String(12))  # RUNNING / SUCCEEDED / FAILED
    started_at: Mapped[datetime] = mapped_column(TS, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(TS)
    object_count: Mapped[int] = mapped_column(Integer, server_default="0")
    copied_count: Mapped[int] = mapped_column(Integer, server_default="0")
    skipped_count: Mapped[int] = mapped_column(Integer, server_default="0")
    total_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0")
    copied_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0")
    table_count: Mapped[int | None] = mapped_column(Integer)
    manifest_key: Mapped[str | None] = mapped_column(String(400))
    db_dump_key: Mapped[str | None] = mapped_column(String(400))
    db_dump_sha256: Mapped[str | None] = mapped_column(String(64))
    failed_objects: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    error: Mapped[str | None] = mapped_column(String(1000))
    restore_test: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class WebhookSubscription(Base):
    """웹훅 구독 (04번 요구사항 8). 서명 비밀은 저장하지 않고 salt로 서버 키에서 다시 만든다 (ADR-0030)."""

    __tablename__ = "webhook_subscriptions"
    webhook_id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.organization_id"), index=True)
    url: Mapped[str] = mapped_column(String(500))
    event_types: Mapped[list[str]] = mapped_column(ARRAY(String(40)))
    secret_salt: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    consecutive_failures: Mapped[int] = mapped_column(Integer, server_default="0")
    disabled_at: Mapped[datetime | None] = mapped_column(TS)
    disabled_reason: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS)
    deleted_at: Mapped[datetime | None] = mapped_column(TS)


class WebhookDelivery(Base):
    """보낼 웹훅 (outbox). 상태를 바꾸는 같은 트랜잭션에서 만들고 워커가 보낸다 (04번 요구사항 8.2·8.3)."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (Index("ix_webhook_deliveries_due", "status", "next_attempt_at"),)
    delivery_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(26), index=True)
    webhook_id: Mapped[str] = mapped_column(ForeignKey("webhook_subscriptions.webhook_id"), index=True)
    organization_id: Mapped[str] = mapped_column(ID)
    event_type: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # PENDING / SENDING / DELIVERED / FAILED / CANCELLED.
    # SENDING은 워커가 가져가 보내는 중(임대) — 내부 상태라 API로 내보내지 않는다
    status: Mapped[str] = mapped_column(String(12))
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    # PENDING: 다음 시도 시각. SENDING: 임대 만료 시각 (지나면 워커가 죽은 것으로 보고 다시 보낸다, ADR-0041)
    next_attempt_at: Mapped[datetime] = mapped_column(TS)
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(TS)
    delivered_at: Mapped[datetime | None] = mapped_column(TS)


class UsageSnapshot(Base):
    """기관별 일별 사용량 스냅숏 (08번 요구사항 8 용량 예측, 10 월간 리포트). 워커가 하루 한 번 쓴다."""

    __tablename__ = "usage_snapshots"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    organization_id: Mapped[str] = mapped_column(ID, primary_key=True)
    stored_bytes: Mapped[int] = mapped_column(BigInteger)
    quarantine_bytes: Mapped[int] = mapped_column(BigInteger)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger)
    backup_bytes: Mapped[int] = mapped_column(BigInteger)  # 기관이 신청한 백업 (사용량에 포함)
    by_zone: Mapped[dict[str, Any]] = mapped_column(JSONB)  # 존별 활성 자산 크기 합
    by_tier: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # 계층별 (0022부터. 그 전 스냅숏은 모두 HOT)
    backup_target_bytes: Mapped[int] = mapped_column(BigInteger)  # 독립 백업 대상 (ADR-0021 술어)
    created_at: Mapped[datetime] = mapped_column(TS)
