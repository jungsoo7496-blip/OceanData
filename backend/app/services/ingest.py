"""수집 유스케이스 (04번): 업로드 생성·재개·완료·취소, 무결성 검증, 격리, 영수증, 쿼터.

바이너리는 API를 거치지 않고 사전 서명 URL로 스토리지에 직접 간다 (요구사항 1.2).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.container import Container
from app.db.models import (
    Asset,
    AssetLineage,
    Device,
    IntegrityLogEntry,
    Organization,
    OrgQuota,
    Project,
    SessionDevice,
    TrialSession,
    Upload,
)
from app.domain import metadata, states
from app.domain.ids import new_id
from app.domain.normalize import TimeRuleError, check_location, resolve_capture_time
from app.domain.principal import Principal
from app.errors import ProblemError
from app.ports.notifier import Notification
from app.ports.storage import CompletedPart
from app.services import contracts
from app.services.quality import apply_rules, load_rules
from app.services.tenant import NotFoundInScope, get_scoped
from app.timefmt import iso

log = logging.getLogger(__name__)
GENESIS = "0" * 64
EXT_KEYS = ("video_ext", "cctv_ext", "vessel_ext", "sensor_ext", "external_obs_ext", "annotation_ext", "evidence_ext")
RELATION_FOR_VARIANT = {"PROXY": "PROXY", "CLIP": "CLIP", "SAMPLE_FRAME": "THUMBNAIL", "THUMBNAIL": "THUMBNAIL"}
_INTEGRITY_LOCK = 7_202_612


def zone_for(variant: str) -> str:
    return "RAW" if variant == "ORIGINAL" else "PROCESSED"


def object_key(a: Asset) -> str:
    """{organization_id}/{project_id}/{session_id}/{source_system_id}/{asset_id}/{variant} (domain.md 9장)."""
    return f"{a.organization_id}/{a.project_id}/{a.session_id or '_unassigned'}/{a.source_system_id}/" \
           f"{a.asset_id}/{a.variant}"


def actor_roles(p: Principal) -> frozenset[str]:
    return frozenset({"DEVICE"}) if p.is_machine else p.roles


# ---- 쿼터 (domain.md 9장)
class Quota:
    def __init__(self, c: Container, db: Session) -> None:
        self.c, self.db = c, db

    def _row(self, org: str) -> OrgQuota:
        self.db.execute(insert(OrgQuota).values(
            organization_id=org, quota_bytes=self.c.settings.default_org_quota_bytes, used_bytes=0,
            quarantine_bytes=0, reserved_bytes=0, backup_bytes=0, updated_at=self.c.clock.now(),
        ).on_conflict_do_nothing(index_elements=["organization_id"]))
        # 행 잠금으로 동시 예약을 직렬화한다 (요구사항 1.5)
        return self.db.execute(select(OrgQuota).where(OrgQuota.organization_id == org).with_for_update()) \
            .scalar_one()

    def require_room(self, org: str, nbytes: int | None = None) -> OrgQuota:
        """행을 잠그고 쿼터 여유를 확인한다 — 업로드 예약과 구간 클립(09번 7.2)이 같은 규칙을 쓴다.

        사용량(저장 + 격리 + 예약 + 신청 백업)에 nbytes를 더해 쿼터를 넘으면 409 QUOTA_EXCEEDED.
        크기를 아직 모르면(None) 여유가 1바이트도 없을 때만 거절한다."""
        q = self._row(org)
        total = q.used_bytes + q.quarantine_bytes + q.reserved_bytes + q.backup_bytes
        if total + (1 if nbytes is None else nbytes) > q.quota_bytes:
            extra: dict[str, Any] = {"quota_bytes": q.quota_bytes, "usage_bytes": total}
            if nbytes is not None:
                extra["requested_bytes"] = nbytes
            raise ProblemError("QUOTA_EXCEEDED", detail="기관 저장 쿼터를 넘습니다", extra=extra)
        return q

    def reserve(self, org: str, nbytes: int) -> None:
        q = self.require_room(org, nbytes)
        q.reserved_bytes += nbytes
        q.updated_at = self.c.clock.now()
        from app.services.retention import check_quota_alerts

        check_quota_alerts(self.c, self.db, q)  # 80·90·100% 알림 (08번 요구사항 2.4)

    def settle(self, org: str, reserved: int, *, to: str | None, actual: int | None = None) -> None:
        """예약을 확정(used)·격리(quarantine)로 옮기거나(to=None이면) 해제한다."""
        q = self._row(org)
        q.reserved_bytes = max(0, q.reserved_bytes - reserved)
        if to == "used":
            q.used_bytes += actual if actual is not None else reserved
        elif to == "quarantine":
            q.quarantine_bytes += actual if actual is not None else reserved
        q.updated_at = self.c.clock.now()
        from app.services.retention import check_quota_alerts

        check_quota_alerts(self.c, self.db, q)


@dataclass
class Resolved:
    device: Device
    project_id: str
    session_id: str | None


class IngestService:
    def __init__(self, c: Container, db: Session) -> None:
        self.c, self.db = c, db
        self.quota = Quota(c, db)

    # ---------------------------------------------------------------- 생성
    def _scoped(self, model: Any, ident: str, p: Principal) -> Any:
        try:
            return get_scoped(self.db, model, ident, p)
        except NotFoundInScope as exc:
            if exc.cross_org:
                self.c.audit.record("CROSS_ORG_ACCESS_ATTEMPT", result="DENIED", principal=p,
                                    organization_id=exc.owner_org, target_type=exc.model, target_id=ident)
            raise ProblemError("NOT_FOUND", detail=f"{exc.model}를 찾을 수 없습니다") from None

    def _resolve(self, p: Principal, m: dict[str, Any], capture_start: datetime | None) -> Resolved:
        source_id = m["source_system_id"]
        if p.kind == "DEVICE" and source_id not in p.mapped_source_ids:  # 매핑된 원천 장비만 (02번 요구사항 5.5)
            self.c.audit.record("DEVICE_SCOPE_VIOLATION", result="DENIED", principal=p,
                                organization_id=p.organization_id, target_type="Device", target_id=source_id)
            raise ProblemError("NOT_FOUND", detail="원천 장비를 찾을 수 없습니다")
        device: Device = self._scoped(Device, source_id, p)
        if device.status == "REVOKED":
            raise ProblemError("RULE_VIOLATION", detail="폐기된 장비입니다",
                               unmet_conditions=[{"condition": "device.status=ACTIVE", "actual": device.status}])
        session_id = None
        session: TrialSession | None = None
        if m.get("session_id"):
            session = self._scoped(TrialSession, m["session_id"], p)
        elif m.get("session_label"):
            session = self.db.execute(select(TrialSession).where(
                TrialSession.organization_id == p.organization_id, TrialSession.label == m["session_label"])) \
                .scalar_one_or_none()
        elif capture_start is not None:
            # 회차 추출 규칙: 장비가 연결된 회차 중 촬영시각을 포함하는 회차가 정확히 하나면 그것 (요구사항 6.1)
            cands = self.db.execute(
                select(TrialSession).join(SessionDevice, SessionDevice.session_id == TrialSession.session_id)
                .where(SessionDevice.device_id == device.device_id, TrialSession.starts_at <= capture_start,
                       (TrialSession.ends_at.is_(None)) | (TrialSession.ends_at >= capture_start))).scalars().all()
            session = cands[0] if len(cands) == 1 else None
        if session is not None:
            session_id = session.session_id
        project_id = m.get("project_id") or (session.project_id if session else None) or device.project_id
        if not project_id:
            raise ProblemError("RULE_VIOLATION", detail="실증사업을 정할 수 없습니다",
                               unmet_conditions=[{"condition": "project_id 또는 장비의 사업", "actual": None}])
        self._scoped(Project, project_id, p)
        if session is not None and session.project_id != project_id:
            raise ProblemError("RULE_VIOLATION", detail="회차가 다른 실증사업에 속합니다",
                               unmet_conditions=[{"condition": "session.project_id=project_id",
                                                  "actual": session.project_id}])
        return Resolved(device, project_id, session_id)

    def _validate_manifest(self, m: Any) -> None:
        problems, version_rejected = contracts.validate_document("upload-manifest", m)
        if version_rejected:
            raise ProblemError("UNSUPPORTED_SCHEMA_VERSION", detail=problems[0].message)
        errors = [p for p in problems if p.severity == "ERROR"]
        if errors:
            raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": p.pointer, "code": p.code,
                                                            "message": p.message} for p in errors])

    def _location(self, m: dict[str, Any]) -> tuple[dict | None, dict | None]:
        """EPSG:4326이 아닌 원래 좌표는 PostGIS로 변환하고 변환 이력을 남긴다 (03번 요구사항 6.3)."""
        if m.get("location") is not None or not m.get("source_location"):
            return m.get("location"), None
        src = m["source_location"]
        srid = int(src["crs"].split(":")[1])
        try:
            g = self.db.execute(text("SELECT ST_AsGeoJSON(ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(:g), :s), 4326))"),
                                {"g": json.dumps(src["geometry"]), "s": srid}).scalar_one()
        except Exception:  # noqa: BLE001 - 잘못된 좌표계·형식은 입력 오류
            self.db.rollback()
            raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/source_location", "code": "transform_failed",
                                                            "message": "좌표계를 변환할 수 없습니다"}]) from None
        geometry = json.loads(g) if g is not None else None
        pointer = "/source_location/geometry"
        if contracts.schema_problems("common", geometry, fragment="#/$defs/geometry"):
            raise ProblemError("VALIDATION_ERROR", errors=[{
                "pointer": pointer, "code": "transform_failed",
                "message": "변환된 위치의 GeoJSON 형식이 올바르지 않습니다",
            }])
        problems = check_location(geometry, pointer)
        if problems:
            raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": p.pointer, "code": p.code,
                                                            "message": p.message} for p in problems])
        return geometry, {"from_crs": src["crs"], "to_crs": "EPSG:4326", "method": "ST_Transform"}

    def create_upload(self, p: Principal, m: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        self._validate_manifest(m)
        try:
            cap = resolve_capture_time(m.get("capture_start_at"), m.get("capture_end_at"), m.get("timestamp_quality"),
                                       m.get("original_timestamps"))
        except TimeRuleError as exc:
            raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/capture_start_at", "code": "time_rule",
                                                            "message": str(exc)}]) from None
        r = self._resolve(p, m, cap.capture_start_at)
        f = m["file"]
        dtype, variant = m["data_type"], m["variant"]
        mode = r.device.collection_mode

        existing = self.db.execute(select(Asset).where(
            Asset.organization_id == p.organization_id, Asset.source_system_id == r.device.device_id,
            Asset.checksum_sha256 == f["checksum_sha256"], Asset.deleted_at.is_(None)).with_for_update()) \
            .scalar_one_or_none()
        now = self.c.clock.now()
        if existing is not None and existing.has_binary:
            active = self.db.execute(select(Upload).where(Upload.asset_id == existing.asset_id,
                                                          Upload.status == "UPLOADING")).scalar_one_or_none()
            if active is None:
                return 200, {"status": "ALREADY_REGISTERED", "asset_id": existing.asset_id}
            if active.expires_at > now:  # 같은 파일의 진행 중 세션을 다시 돌려준다
                return 201, self._created_view(active, existing)
            # 완료·취소는 세션을 먼저 잠근다. 자산을 잡은 이 경로는 세션 잠금을 기다리지 않는다.
            locked = self.db.scalar(select(Upload.upload_id).where(
                Upload.upload_id == active.upload_id, Upload.status == "UPLOADING").with_for_update(skip_locked=True))
            if locked is None:
                raise ProblemError("CONFLICT", detail="기존 업로드를 처리 중입니다. 잠시 뒤 다시 시도하세요")
            self._release(active, "EXPIRED", require_cleanup=True)
            if not active.asset_was_metadata_only:
                existing = None  # 바이너리를 받은 적 없는 신규 자산 기록은 삭제됐다

        if mode == "C0_METADATA_ONLY":
            return self._register_metadata_only(p, m, r, cap, existing)
        if not metadata.variant_allowed(mode, dtype, variant):
            allowed = sorted(metadata.MEDIA_VARIANTS_BY_MODE[mode]) if dtype in metadata.MEDIA_TYPES else ["ORIGINAL"]
            raise ProblemError("RULE_VIOLATION", detail=f"{mode} 장비는 이 variant를 보낼 수 없습니다",
                               unmet_conditions=[{"condition": f"variant ∈ {{{', '.join(allowed)}}}",
                                                  "actual": variant}], extra={"allowed_variants": allowed})

        location, transform = self._location(m)
        if existing is not None:  # 바이너리 없는 C0 자산 → 같은 asset_id로 세션을 연다 (요구사항 2.4)
            asset = existing
            asset.ingest_status = states.transition("ingest_status", asset.ingest_status, "UPLOADING", actor_roles(p))
            asset.variant, asset.has_binary, asset.updated_at = variant, True, now
            was_c0 = True
        else:
            asset = self._new_asset(p, m, r, cap, location, transform, has_binary=True,
                                    status=states.transition("ingest_status", None, "UPLOADING", actor_roles(p)))
            was_c0 = False
        size = int(f["byte_size"])
        self.quota.reserve(p.organization_id, size)
        part_size = max(self.c.settings.upload_part_size, math.ceil(size / 10000) if size else 1)
        part_count = max(1, math.ceil(size / part_size))
        zone = zone_for(variant)
        bucket = self.c.settings.buckets[zone]
        key = object_key(asset)
        try:
            s3_upload_id = self.c.storage.create_multipart_upload(bucket, key, f.get("mime_type"))
        except Exception as exc:  # noqa: BLE001 - 스토리지 장애는 503으로 (요구사항 1.6)
            log.warning("storage_unavailable", extra={"error_type": type(exc).__name__})
            raise ProblemError("STORAGE_UNAVAILABLE", headers={"Retry-After": "30"}) from None
        asset.storage_zone, asset.storage_key = zone, key
        up = Upload(upload_id=new_id("upload"), organization_id=p.organization_id, asset_id=asset.asset_id,
                    source_system_id=r.device.device_id, created_by=p.principal_id, bucket=bucket, object_key=key,
                    s3_upload_id=s3_upload_id, part_size=part_size, part_count=part_count, declared_size=size,
                    reserved_bytes=size, status="UPLOADING", asset_was_metadata_only=was_c0,
                    expires_at=now + timedelta(seconds=self.c.settings.upload_session_ttl_s), created_at=now,
                    updated_at=now)
        self.db.add(up)
        self.db.flush()
        return 201, self._created_view(up, asset)

    def _new_asset(self, p: Principal, m: dict[str, Any], r: Resolved, cap: Any, location: dict | None,
                   transform: dict | None, *, has_binary: bool, status: str) -> Asset:
        now = self.c.clock.now()
        f = m["file"]
        fields = metadata.apply_defaults({"data_role": m.get("data_role")},
                                         device_collection_mode=r.device.collection_mode)
        parent = None
        if m.get("parent_ref", {}).get("checksum_sha256"):
            parent = self.db.execute(select(Asset).where(
                Asset.organization_id == p.organization_id, Asset.source_system_id == r.device.device_id,
                Asset.checksum_sha256 == m["parent_ref"]["checksum_sha256"], Asset.deleted_at.is_(None))) \
                .scalar_one_or_none()
            if parent is not None:  # 파생본은 원 자산의 엄격한 값을 물려받는다 (domain.md 3장)
                fields.update(metadata.inherit_strictest([
                    {"data_role": fields["data_role"], "sensitivity_class": fields["sensitivity_class"]},
                    {"data_role": parent.data_role, "sensitivity_class": parent.sensitivity_class}]))
        kind = (m.get("client") or {}).get("kind", "API")
        provenance: dict[str, Any] = {"ingest_channel": kind, "client_version": m["client"]["version"],
                                      "original_filename": f["original_filename"]}
        if f.get("original_relative_path"):
            provenance["original_relative_path"] = f["original_relative_path"]
        if cap.original_timestamps:
            provenance["original_timestamps"] = cap.original_timestamps
        if parent is not None:
            provenance["parent_asset_ids"] = [parent.asset_id]
            provenance["transform"] = RELATION_FOR_VARIANT.get(m["variant"], m["variant"])
        asset = Asset(
            asset_id=new_id("asset"), schema_version=m["schema_version"], organization_id=p.organization_id,
            project_id=r.project_id, session_id=r.session_id, source_system_id=r.device.device_id,
            data_type=m["data_type"], variant=m["variant"], capture_start_at=cap.capture_start_at,
            capture_end_at=cap.capture_end_at, ingested_at=now, timestamp_quality=cap.timestamp_quality,
            location=location, source_location=m.get("source_location"), area_code=m.get("area_code"),
            location_precision="EXACT_RESTRICTED" if location else None, mime_type=f["mime_type"],
            byte_size=int(f["byte_size"]), checksum_sha256=f["checksum_sha256"], has_binary=has_binary,
            retention_policy_id="default", retention_until=now + timedelta(days=self.c.settings.default_retention_days),
            quality_reasons=[], ingest_status=status, provenance=provenance,
            ext={k: m[k] for k in EXT_KEYS if k in m},
            ingest_context={k: v for k, v in {"watch_rule_id": m.get("watch_rule_id"),
                                              "local_collection_id": (m.get("client") or {}).get("local_collection_id"),
                                              "parent_ref": m.get("parent_ref"), "location_transform": transform,
                                              "session_label": m.get("session_label"),
                                              "uploaded_by": p.principal_id}.items() if v},
            created_at=now, updated_at=now, **fields)
        from app.services.retention import apply_retention

        apply_retention(self.db, asset, now)  # 보존 정책 표 (08번 요구사항 3.2)
        self.db.add(asset)
        self.db.flush()
        if parent is not None:
            self.db.add(AssetLineage(child_asset_id=asset.asset_id, parent_asset_id=parent.asset_id,
                                     relation=RELATION_FOR_VARIANT.get(m["variant"], "PROXY"),
                                     tool_version=f"{kind} {m['client']['version']}", run_by=p.principal_id,
                                     created_at=now))
        return asset

    def _register_metadata_only(self, p: Principal, m: dict[str, Any], r: Resolved, cap: Any,
                                existing: Asset | None) -> tuple[int, dict[str, Any]]:
        """C0: 바이너리 없이 메타데이터·현장 파일 크기·sha256만 등록 (요구사항 1.3)."""
        if existing is not None:
            return 200, {"status": "ALREADY_REGISTERED", "asset_id": existing.asset_id}
        if m["variant"] != "ORIGINAL":
            raise ProblemError("RULE_VIOLATION", detail="C0 장비는 원본 기록만 등록합니다",
                               unmet_conditions=[{"condition": "variant=ORIGINAL", "actual": m["variant"]}])
        if r.session_id is None:  # 회차 없는 자산은 등록 상태가 될 수 없다 (domain.md 8장)
            raise ProblemError("RULE_VIOLATION", detail="메타데이터 전용 등록에는 실증회차가 필요합니다",
                               unmet_conditions=[{"condition": "session_id 또는 session_label", "actual": None}])
        location, transform = self._location(m)
        rules = load_rules(self.db)
        asset = self._new_asset(p, m, r, cap, location, transform, has_binary=False,
                                status=states.transition("ingest_status", None, "READY_PRIVATE", actor_roles(p)))
        apply_rules(asset, rules, self.c.clock.now())
        return 201, {"status": "METADATA_ONLY_REGISTERED", "asset_id": asset.asset_id}

    @staticmethod
    def _part_len(up: Upload, n: int) -> int:
        """계획한 n번 파트 크기: 마지막 파트만 나머지 (신고 byte_size를 part_size로 나눈 계획)."""
        return max(0, min(up.part_size, up.declared_size - (n - 1) * up.part_size))

    def _part_urls(self, up: Upload, numbers: list[int]) -> list[dict[str, Any]]:
        ttl = self.c.settings.presign_upload_ttl_s
        exp = iso(self.c.clock.now() + timedelta(seconds=ttl))
        limit = self.c.settings.max_part_urls_per_response
        # 파트마다 계획한 크기를 Content-Length로 서명한다: 신고보다 큰 본문은 저장소가 거부한다 (쿼터 우회 방지)
        return [{"part_number": n, "url": self.c.storage.presign_upload_part(
                    up.bucket, up.object_key, up.s3_upload_id, n, ttl, content_length=self._part_len(up, n)),
                 "expires_at": exp} for n in numbers[:limit]]

    def _created_view(self, up: Upload, asset: Asset) -> dict[str, Any]:
        return {"status": "UPLOAD_CREATED", "upload_id": up.upload_id, "asset_id": asset.asset_id,
                "part_size": up.part_size, "part_count": up.part_count,
                "parts": self._part_urls(up, list(range(1, up.part_count + 1))), "expires_at": iso(up.expires_at)}

    # ---------------------------------------------------------------- 조회·재개·취소
    def load_upload(self, p: Principal, upload_id: str) -> Upload:
        up: Upload = self._scoped(Upload, upload_id, p)
        if p.kind == "DEVICE" and up.source_system_id not in p.mapped_source_ids:
            raise ProblemError("NOT_FOUND")
        return up

    def _expire_if_needed(self, up: Upload) -> None:
        if up.status == "UPLOADING" and up.expires_at <= self.c.clock.now() and self._release(up, "EXPIRED"):
            self.db.commit()  # 410을 돌려주기 전에 정리를 확정한다 (요구사항 3.3)
            raise ProblemError("GONE", detail="업로드 세션이 만료되었습니다")
        if up.status in ("EXPIRED", "ABORTED"):
            raise ProblemError("GONE", detail="만료되었거나 취소된 업로드입니다")

    def _release(
        self, up: Upload, final: str, *, require_cleanup: bool = False, preserve_asset: bool = False,
    ) -> bool:
        self.db.refresh(up, with_for_update=True)
        if up.status != "UPLOADING":
            return False
        try:
            self.c.storage.abort_multipart_upload(up.bucket, up.object_key, up.s3_upload_id)
            completed_object = preserve_asset and self.c.storage.head_object(up.bucket, up.object_key) is not None
        except Exception as exc:  # noqa: BLE001 - 이미 정리된 멀티파트일 수 있다
            log.warning("abort_multipart_failed", extra={"error_type": type(exc).__name__})
            if require_cleanup or preserve_asset:
                raise ProblemError("STORAGE_UNAVAILABLE", headers={"Retry-After": "30"}) from None
        else:
            if completed_object:
                raise ProblemError("CONFLICT", detail="저장소에 완료 객체가 있어 업로드 중단을 확인할 수 없습니다")
        self.quota.settle(up.organization_id, up.reserved_bytes, to=None)
        up.status, up.reserved_bytes, up.updated_at = final, 0, self.c.clock.now()
        if preserve_asset:
            up.asset_id = None  # 삭제 경로에서 자산 기록에 삭제 증명을 연결한다.
        asset = self.db.get(Asset, up.asset_id) if up.asset_id else None
        if asset is not None and asset.ingest_status == "UPLOADING":
            if up.asset_was_metadata_only:
                # 바이너리 없는 C0 자산으로 되돌린다 (domain.md 5장 표에 없는 역전이 — 확인 필요, tasks 참고)
                asset.ingest_status, asset.has_binary, asset.variant = "READY_PRIVATE", False, "ORIGINAL"
                # 객체는 만들어진 적이 없다 — 저장 위치를 남기면 삭제 증명에 없는 객체가 올라간다
                asset.storage_zone, asset.storage_key = None, None
            else:
                # 받은 것이 없는 새 자산 기록은 지운다. 같은 파일을 다시 올릴 수 있게 한다.
                # parent_ref로 만든 계보 행(이 자산이 파생본이거나, 진행 중인 다른 업로드가 이 자산을 원본으로
                # 지목한 경우)이 남아 있으면 외래 키 때문에 지울 수 없으므로 먼저 지운다 (받은 바이트가 없는 계보)
                self.db.execute(delete(AssetLineage).where(or_(AssetLineage.child_asset_id == asset.asset_id,
                                                               AssetLineage.parent_asset_id == asset.asset_id)))
                self.db.delete(asset)
        self.db.flush()
        return True

    def upload_status(self, up: Upload) -> dict[str, Any]:
        asset = self.db.get(Asset, up.asset_id) if up.asset_id else None
        done: list[CompletedPart] = []
        if up.status == "UPLOADING" and up.expires_at > self.c.clock.now():
            try:
                done = self.c.storage.list_parts(up.bucket, up.object_key, up.s3_upload_id)
            except Exception:  # noqa: BLE001
                done = []
        out: dict[str, Any] = {
            "upload_id": up.upload_id,
            "ingest_status": asset.ingest_status if asset else "FAILED",
            "parts_completed": up.part_count if up.status == "RECEIVED" else len(done),
            "part_count": up.part_count, "error_code": up.error_code, "expires_at": iso(up.expires_at),
            "created_at": iso(up.created_at),
        }
        if up.asset_id:
            out["asset_id"] = up.asset_id
        if up.status == "RECEIVED":
            out["bytes_received"] = asset.byte_size if asset else up.declared_size
        if asset is not None and asset.quarantine_reason:
            out["quarantine_reason"] = asset.quarantine_reason
        return out

    def list_parts(self, up: Upload) -> dict[str, Any]:
        self._expire_if_needed(up)
        if up.status != "UPLOADING":
            raise ProblemError("CONFLICT", detail="이미 완료된 업로드입니다")
        try:
            done = self.c.storage.list_parts(up.bucket, up.object_key, up.s3_upload_id)
        except Exception:  # noqa: BLE001
            raise ProblemError("STORAGE_UNAVAILABLE", headers={"Retry-After": "30"}) from None
        got = {p.part_number for p in done}
        pending = [n for n in range(1, up.part_count + 1) if n not in got]
        sizes = {p.part_number: p for p in done}
        return {"upload_id": up.upload_id, "part_size": up.part_size, "part_count": up.part_count,
                "completed_parts": [{"part_number": n, "etag": sizes[n].etag,
                                     "size": min(up.part_size, up.declared_size - (n - 1) * up.part_size)}
                                    for n in sorted(got)],
                "pending_parts": self._part_urls(up, pending), "expires_at": iso(up.expires_at)}

    def sweep_expired(self, limit: int = 100) -> int:
        """만료된 업로드 세션 정리: 멀티파트 정리·예약 해제 (요구사항 3.3). 워커가 주기적으로 부른다.

        세션마다 SAVEPOINT 안에서 정리한다. 한 행이 실패해도(기록하고 넘어간다) 같은 묶음의 다른 기관 세션
        정리와 그 뒤 주기 작업이 막히지 않는다. 돌려주는 값은 정리에 성공한 세션 수다.
        """
        rows = self.db.execute(select(Upload).where(Upload.status == "UPLOADING",
                                                    Upload.expires_at <= self.c.clock.now())
                               .order_by(Upload.expires_at).limit(limit).with_for_update(skip_locked=True)) \
            .scalars().all()
        done = 0
        for up in rows:
            upload_id = up.upload_id
            try:
                with self.db.begin_nested():
                    released = self._release(up, "EXPIRED")
            except Exception as exc:  # noqa: BLE001 - 한 세션의 실패가 묶음 전체를 막지 않게 한다
                log.error("expired_upload_release_failed", extra={"upload_id": upload_id,
                                                                  "error_type": type(exc).__name__})
                continue
            done += int(released)
        return done

    def abort(self, up: Upload, *, preserve_asset: bool = False) -> None:
        if not self._release(up, "ABORTED", preserve_asset=preserve_asset):
            raise ProblemError("CONFLICT", detail="진행 중인 업로드만 취소할 수 있습니다")

    def _check_part_sizes(self, up: Upload) -> dict[int, CompletedPart]:
        """저장소에 올라간 파트 크기가 계획(신고 byte_size)과 같아야 완료한다.

        쿼터는 신고 크기로 예약하므로, 더 큰 파트를 올려 완료하면 쿼터 밖 바이트가 남는다. 다르면 세션을 취소하고
        (멀티파트 정리·예약 해제, ABORTED) 409로 알린다. 일치한 파트는 ETag와 함께 반환한다.
        """
        try:
            stored = self.c.storage.list_parts(up.bucket, up.object_key, up.s3_upload_id)
        except Exception:  # noqa: BLE001
            raise ProblemError("STORAGE_UNAVAILABLE", headers={"Retry-After": "30"}) from None
        if any(part.size is None or part.size < 0 or not 1 <= part.part_number <= 10000 for part in stored):
            raise ProblemError("STORAGE_UNAVAILABLE", detail="저장소의 파트 크기를 확인하지 못했습니다",
                               headers={"Retry-After": "30"})
        wrong = [{"part_number": s.part_number, "expected_size": self._part_len(up, s.part_number),
                  "actual_size": s.size} for s in sorted(stored, key=lambda s: s.part_number)
                 if s.part_number > up.part_count or s.size != self._part_len(up, s.part_number)]
        if not wrong:
            return {part.part_number: part for part in stored}
        log.warning("upload_part_size_mismatch", extra={"upload_id": up.upload_id, "parts": len(wrong)})
        self._release(up, "ABORTED", require_cleanup=True)
        self.db.commit()  # 409를 돌려주기 전에 취소·예약 해제를 확정한다
        raise ProblemError("CONFLICT", detail="올린 파트 크기가 신고한 파일 크기(byte_size)와 다릅니다. 업로드를 "
                                              "취소했습니다 — 새 업로드로 다시 보내세요",
                           extra={"size_mismatch": wrong[:100]})

    def complete(self, p: Principal, up: Upload, parts: list[dict[str, Any]]) -> dict[str, Any]:
        self.db.refresh(up, with_for_update=True)
        self._expire_if_needed(up)
        if up.status == "RECEIVED":
            return self.upload_status(up)  # 같은 완료 요청 재시도
        if up.status != "UPLOADING":
            raise ProblemError("CONFLICT", detail="진행 중인 업로드가 아닙니다")
        numbers = sorted(int(x["part_number"]) for x in parts)
        if numbers != list(range(1, up.part_count + 1)):
            raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/parts", "code": "incomplete_parts",
                                                            "message": f"파트 1~{up.part_count}이 모두 필요합니다"}])
        if self._completed_in_storage(up):
            # 앞선 완료 요청이 저장소에서 멀티파트를 끝냈는데 DB 커밋이 실패했다 — 같은 요청의 재시도다.
            # 파트를 다시 맞추지 않고 DB 전이만 마친다. (전에는 완료된 멀티파트를 조회하지 못해 재시도가 503만 받았다)
            return self._mark_received(p, up)
        stored = self._check_part_sizes(up)
        verified = []
        mismatch = "파트 목록이 저장소와 맞지 않습니다 (GET .../parts로 확인)"
        for part in parts:
            saved = stored.get(int(part["part_number"]))
            if saved is None or saved.etag.strip('"') != part["etag"].strip('"'):
                raise ProblemError("CONFLICT", detail=mismatch)
            verified.append(saved)
        try:
            self.c.storage.complete_multipart_upload(up.bucket, up.object_key, up.s3_upload_id, verified)
        except Exception as exc:  # noqa: BLE001 - 잘못된 ETag·누락 파트
            log.warning("complete_multipart_failed", extra={"error_type": type(exc).__name__})
            raise ProblemError("CONFLICT", detail=mismatch) from None
        return self._mark_received(p, up)

    def _completed_in_storage(self, up: Upload) -> bool:
        """저장소에 신고 크기의 완성 객체가 이미 있는가. 객체는 멀티파트가 완료돼야 생기고 그 키는 이 업로드만 쓴다."""
        try:
            info = self.c.storage.head_object(up.bucket, up.object_key)
        except Exception:  # noqa: BLE001 - 확인하지 못하면 보통 경로가 파트 목록 조회로 판단한다
            return False
        return info is not None and info.size == up.declared_size

    def _mark_received(self, p: Principal, up: Upload) -> dict[str, Any]:
        asset = self.db.get(Asset, up.asset_id)
        assert asset is not None
        asset.ingest_status = states.transition("ingest_status", asset.ingest_status, "RECEIVED", actor_roles(p))
        asset.updated_at = up.updated_at = self.c.clock.now()
        up.status = "RECEIVED"
        self.db.flush()
        self.c.queue.enqueue("integrity_verify", {"asset_id": asset.asset_id, "upload_id": up.upload_id},
                             organization_id=asset.organization_id,
                             dedupe_key=f"integrity_verify:{asset.asset_id}:{up.upload_id}")
        return self.upload_status(up)


# ---------------------------------------------------------------- 무결성 검증 (워커)
MAGIC = [
    (4, b"ftyp", "video/mp4"),
    (0, b"\x1aE\xdf\xa3", "video/x-matroska"),
    (0, b"\xff\xd8\xff", "image/jpeg"),
    (0, b"\x89PNG\r\n\x1a\n", "image/png"),
    (0, b"%PDF", "application/pdf"),
    (0, b"PK\x03\x04", "application/zip"),
    (0, b"RIFF", "riff"),
]
FAMILY = {"video/mp4": {"video/mp4", "video/quicktime"}, "video/x-matroska": {"video/x-matroska", "video/webm"},
          "image/jpeg": {"image/jpeg"}, "image/png": {"image/png"}, "application/pdf": {"application/pdf"},
          "application/zip": {"application/zip"}, "riff": {"video/x-msvideo", "audio/wav", "image/webp"},
          "text": {"text/csv", "application/json", "text/plain", "application/x-ndjson", "application/geo+json"}}


def sniff(head: bytes) -> str | None:
    """내용(매직 바이트)으로 형식을 판별한다 (security.md: 신고 MIME을 믿지 않는다)."""
    for offset, sig, mime in MAGIC:
        if head[offset:offset + len(sig)] == sig:
            return mime
    try:
        head.decode("utf-8")
        return "text" if head and b"\x00" not in head else None
    except UnicodeDecodeError:
        # 잘린 다중 바이트 문자일 수 있다
        try:
            head[:-3].decode("utf-8")
            return "text"
        except UnicodeDecodeError:
            return None


def mime_matches(detected: str | None, declared: str) -> bool:
    return detected is not None and declared.split(";")[0].strip().lower() in FAMILY.get(detected, {detected})


class IntegrityVerifier:
    def __init__(self, c: Container) -> None:
        self.c = c

    def verify(self, asset_id: str) -> str:
        with self.c.sessionmaker.begin() as db:
            asset = db.execute(select(Asset).where(Asset.asset_id == asset_id).with_for_update()).scalar_one()
            revalidate = asset.ingest_status == "VALIDATING" and asset.storage_zone == "QUARANTINE"
            # 앞선 시도가 VALIDATING을 확정한 뒤 죽었거나 실패했으면(임대 만료·스트림 오류) 이어서 끝낸다.
            # 검증은 객체를 다시 읽는 작업이고 등록·정산은 아래 두 번째 트랜잭션 한 번이라 되풀이해도 안전하다(ADR-0058)
            resume = asset.ingest_status == "VALIDATING" and not revalidate
            if asset.ingest_status != "RECEIVED" and not revalidate and not resume:
                return asset.ingest_status  # 이미 처리됨 (작업 재실행)
            # 규칙 오류로 RECEIVED 자산이 VALIDATING에 남지 않도록 전이 전에 한 버전을 고정한다.
            rules = load_rules(db)
            # 기관 정책이 악성코드 검사를 요구하면 검사기가 있어야 한다. 없으면 전이 전에 멈춰 다시 시도한다 (4.7)
            org = db.get(Organization, asset.organization_id)
            scanner = None
            if org is not None and org.malware_scan_required:
                from app.adapters.malware_scanners import ScannerUnavailable, scanner_for

                scanner = scanner_for(self.c.settings)
                if scanner is None:
                    raise ScannerUnavailable("기관 정책이 악성코드 검사를 요구하지만 MALWARE_SCANNER가 none입니다")
            if asset.ingest_status == "RECEIVED":
                asset.ingest_status = states.transition("ingest_status", "RECEIVED", "VALIDATING", {"SERVICE"})
            from app.services import tiers

            storage, bucket = tiers.locate(self.c, asset)
            key = asset.storage_key or ""
            up = db.execute(select(Upload).where(Upload.asset_id == asset_id, Upload.status == "RECEIVED")
                            .order_by(Upload.created_at.desc()).limit(1)).scalar_one_or_none()
        # 스트리밍으로 다시 계산한다 (요구사항 4.2)
        h = hashlib.sha256()
        size = 0
        head = b""
        scan = scanner.start() if scanner is not None else None
        for chunk in storage.iter_object(bucket, key):
            if len(head) < 4096:
                head += chunk[:4096 - len(head)]
            h.update(chunk)
            size += len(chunk)
            if scan is not None:
                scan.feed(chunk)
        digest = h.hexdigest()
        scanned = scan.finish() if scan is not None else None
        with self.c.sessionmaker.begin() as db:
            asset = db.execute(select(Asset).where(Asset.asset_id == asset_id).with_for_update()).scalar_one()
            if asset.ingest_status != "VALIDATING":
                return asset.ingest_status  # 겹쳐 돈 다른 실행이 먼저 등록·격리했다 (임대 만료로 작업이 겹칠 수 있다)
            reason = None
            if scanned is not None:  # 검사 기록 (엔진·결과만, 내용은 남기지 않는다)
                asset.ingest_context = {**(asset.ingest_context or {}), "malware_scan": {
                    "engine": scanned.engine, "result": "CLEAN" if scanned.clean else "SUSPECTED",
                    "signature": scanned.signature, "scanned_at": iso(self.c.clock.now())}}
            if digest != asset.checksum_sha256 or size != asset.byte_size:
                reason = "CHECKSUM_MISMATCH"
            elif scanned is not None and not scanned.clean:
                reason = "MALWARE_SUSPECTED"
            else:
                detected = sniff(head)
                if detected is None:
                    reason = "UNSUPPORTED_FORMAT"
                elif not mime_matches(detected, asset.mime_type):
                    reason = "MIME_MISMATCH"
                elif asset.session_id is None:
                    reason = "UNKNOWN_SESSION"
            quota = Quota(self.c, db)
            reserved = 0 if revalidate or up is None else up.reserved_bytes
            if up is not None and not revalidate:
                db.get(Upload, up.upload_id).reserved_bytes = 0  # type: ignore[union-attr]
            if reason:
                self._quarantine(db, asset, reason)
                if not revalidate:
                    # 격리 사용량은 자산의 byte_size로 센다: 삭제(retention.delete_assets)·재검증이 같은 값을 빼야
                    # 쌓이지 않는다. 완료 시점에 파트 크기를 신고 크기와 맞춰 보므로 받은 객체 크기와도 같다
                    quota.settle(asset.organization_id, reserved, to="quarantine", actual=asset.byte_size)
                return "QUARANTINED"
            if revalidate:  # 격리 존에서 정식 위치(HOT)로 되돌린다
                asset.quarantine_reason = None
                tiers.relocate(self.c, asset, zone=zone_for(asset.variant), key=object_key(asset))
            asset.ingest_status = states.transition("ingest_status", "VALIDATING", "REGISTERED", {"SERVICE"})
            apply_rules(asset, rules, self.c.clock.now())
            asset.updated_at = self.c.clock.now()
            append_integrity(db, asset, self.c.clock.now())
            # 잠금 순서는 다른 파생 작업(record_derived)과 같게 '무결성 로그 → 쿼터 행'이다.
            # 재검증이 쿼터 행을 먼저 잡으면 교착이 나고, 피해자가 되면 객체는 옮겨졌는데 DB는 되돌아간다 (ADR-0058)
            if revalidate:  # 격리 사용량을 사용량으로 옮긴다
                q = quota._row(asset.organization_id)
                q.quarantine_bytes = max(0, q.quarantine_bytes - asset.byte_size)
                q.used_bytes += asset.byte_size
            else:
                quota.settle(asset.organization_id, reserved, to="used", actual=asset.byte_size)  # 통과면 size와 같다
            if asset.data_type != "VIDEO":  # 프록시가 필요 없는 유형 (05번이 영상 프록시를 만든다)
                asset.ingest_status = states.transition("ingest_status", "REGISTERED", "READY_PRIVATE", {"SERVICE"})
            from app.services.pipeline import schedule_after_register

            schedule_after_register(self.c, asset)  # 영상 처리(05번)·센서 파일 파싱(07번)
            return asset.ingest_status

    def _quarantine(self, db: Session, asset: Asset, reason: str) -> None:
        from app.services import tiers

        asset.ingest_status = states.transition("ingest_status", asset.ingest_status, "QUARANTINED", {"SERVICE"})
        asset.quarantine_reason = reason
        asset.updated_at = self.c.clock.now()
        try:
            if asset.storage_zone != "QUARANTINE":  # 격리 존(HOT)으로 옮긴다 (요구사항 5.2, 08번 요구사항 9)
                tiers.relocate(self.c, asset, zone="QUARANTINE", key=asset.storage_key or "")
        except Exception as exc:  # noqa: BLE001 - 이동 실패해도 상태는 격리로 둔다 (재시도는 08번 정리 작업)
            log.error("quarantine_move_failed", extra={"asset_id": asset.asset_id, "error_type": type(exc).__name__})
        # 격리 보존은 사유별 정책 (domain.md 9장: 손상 30일 삭제, 회차 미상·메타 누락은 자동 삭제 없음)
        from app.services.retention import apply_retention

        apply_retention(db, asset, self.c.clock.now())
        uploader = (asset.ingest_context or {}).get("uploaded_by")
        self.c.notifier.send(Notification(kind="upload.quarantined", organization_id=asset.organization_id,
                                          subject="업로드 자산 격리", body=f"자산 {asset.asset_id}: {reason}",
                                          data={"asset_id": asset.asset_id, "reason": reason, "uploader": uploader}))


def integrity_payload(prev: str, asset_id: str, sha: str, size: int, registered_at: datetime) -> str:
    return hashlib.sha256(f"{prev}|{asset_id}|{sha}|{size}|{iso(registered_at)}".encode()).hexdigest()


def append_integrity(db: Session, asset: Asset, now: datetime) -> IntegrityLogEntry:
    """무결성 로그 해시 체인에 추가 (04번 4.6, 05번 5.1: 등록·파생 자산을 같은 체인으로)."""
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _INTEGRITY_LOCK})
    prev = db.execute(text("SELECT entry_hash FROM integrity_log ORDER BY seq DESC LIMIT 1")).scalar() or GENESIS
    registered_at = now.replace(microsecond=0)
    entry = IntegrityLogEntry(asset_id=asset.asset_id, checksum_sha256=asset.checksum_sha256,
                              byte_size=asset.byte_size, registered_at=registered_at, prev_hash=prev,
                              entry_hash=integrity_payload(prev, asset.asset_id, asset.checksum_sha256,
                                                           asset.byte_size, registered_at))
    db.add(entry)
    if asset.variant == "ORIGINAL":  # 파생본 등록은 새 수신이 아니다 (11번 마지막 정상 등록)
        from app.services.monitoring import touch_registered

        touch_registered(db, asset.organization_id, asset.source_system_id, registered_at)
    db.flush()
    return entry


def receipt(db: Session, asset: Asset) -> dict[str, Any] | None:
    e = db.execute(select(IntegrityLogEntry).where(IntegrityLogEntry.asset_id == asset.asset_id)).scalar_one_or_none()
    if e is None:
        return None
    return {"asset_id": asset.asset_id, "checksum_sha256": e.checksum_sha256, "byte_size": e.byte_size,
            "registered_at": iso(e.registered_at), "integrity_log_entry_id": str(e.seq),
            "integrity_entry_hash": e.entry_hash}
