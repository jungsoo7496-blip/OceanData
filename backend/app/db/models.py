"""SQLAlchemy 모델. 컬럼 이름은 snake_case, 시각은 UTC timestamptz (structure.md 3장)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
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
    created_at: Mapped[datetime] = mapped_column(TS)


class User(Base):
    __tablename__ = "users"
    user_id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.organization_id"), index=True)
    idp_subject: Mapped[str] = mapped_column(String(128), unique=True)
    username: Mapped[str] = mapped_column(String(120))
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
        Index("ix_jobs_claim", "status", "priority", "run_after"),
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
    created_at: Mapped[datetime] = mapped_column(TS)
    updated_at: Mapped[datetime] = mapped_column(TS)


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
        UniqueConstraint("organization_id", "source_system_id", "checksum_sha256", name="idem_key"),
        Index("ix_assets_org_status", "organization_id", "ingest_status"),
        Index("ix_assets_location", "location", postgresql_using="gist"),
        Index("ix_assets_capture", "organization_id", "capture_start_at"),
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
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB)
    env_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    tags: Mapped[list[Any] | None] = mapped_column(JSONB)
    ext: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    ingest_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # 내부용(감시 규칙·로컬 ID·좌표 변환 이력)
    storage_zone: Mapped[str | None] = mapped_column(String(20))
    storage_key: Mapped[str | None] = mapped_column(String(400))
    embedding: Mapped[Any | None] = mapped_column(Vector(), deferred=True)
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
    expires_at: Mapped[datetime] = mapped_column(TS, index=True)
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
