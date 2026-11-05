"""기존 품질 규칙 저장소와 자산 재평가 (03번 요구사항 10)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StrictInt, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.container import Container
from app.db.models import Asset, Organization, QualityRuleSet
from app.domain.quality import DEFAULT_RULES, evaluate
from app.services import contracts

_ASSET_FIELDS = (
    "data_type", "mime_type", "byte_size", "checksum_sha256", "timestamp_quality",
    "capture_start_at", "capture_end_at", "session_id", "location", "source_system_id",
)
_EVALUATED_STATES = ("REGISTERED", "PROXIED", "READY_PRIVATE")
_FiniteBound = Annotated[FiniteFloat, Field(strict=True)]


class QualityRuleError(ValueError):
    """운영자가 규칙 또는 재평가 범위를 확인해야 한다."""


class _RuleOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: StrictInt | None = None
    required_fields: dict[str, list[str]] = Field(default_factory=dict)
    capture_time_unknown: Literal["WARNING", "FAILED"] = "WARNING"
    unknown_unit: Literal["WARNING", "FAILED"] = "WARNING"
    not_playable: Literal["FAILED"] = "FAILED"
    value_ranges: dict[str, tuple[_FiniteBound, _FiniteBound]] = Field(default_factory=dict)


@dataclass(frozen=True)
class ActiveRules:
    version: int | None
    overrides: dict[str, Any]


def load_rules(db: Session) -> ActiveRules:
    rows = db.scalars(select(QualityRuleSet).where(QualityRuleSet.active.is_(True)).limit(2)).all()
    if not rows:
        return ActiveRules(None, {})
    if len(rows) != 1:
        raise QualityRuleError("활성 품질 규칙은 한 버전이어야 합니다")
    row = rows[0]
    try:
        parsed = _RuleOverrides.model_validate(row.rules)
    except ValidationError as exc:
        raise QualityRuleError(f"품질 규칙 v{row.version}의 형식이 올바르지 않습니다") from exc
    if row.version < 1 or (parsed.version is not None and parsed.version != row.version):
        raise QualityRuleError("품질 규칙 문서와 DB의 버전이 일치해야 합니다")
    data_types = contracts.schema_files()["common"]["$defs"]["data_type"]["enum"]
    if any(kind not in data_types or not set(fields) <= set(_ASSET_FIELDS)
           for kind, fields in parsed.required_fields.items()):
        raise QualityRuleError("품질 규칙의 데이터 유형 또는 필수 필드를 지원하지 않습니다")
    if any(lo > hi for lo, hi in parsed.value_ranges.values()):
        raise QualityRuleError("품질 규칙 범위의 최소값은 최대값 이하여야 합니다")
    overrides = parsed.model_dump(exclude_unset=True, exclude={"version"})
    if "required_fields" in overrides:
        overrides["required_fields"] = {**DEFAULT_RULES["required_fields"], **parsed.required_fields}
    return ActiveRules(row.version, overrides)


def _known_facts(asset: Asset) -> dict[str, Any]:
    facts = dict((asset.ingest_context or {}).get("quality_facts") or {})
    # 레거시 결과에서 확인 가능한 처리 사실만 복원한다.
    reasons = asset.quality_reasons or []
    if "not_playable" in reasons:
        facts.setdefault("playable", False)
    if "unknown_unit" in reasons:
        facts.setdefault("unknown_units", True)
    return facts


def apply_rules(asset: Asset, rules: ActiveRules, now: datetime, *, facts: dict[str, Any] | None = None) -> bool:
    known = {**_known_facts(asset), **(facts or {})}
    status, reasons = evaluate({name: getattr(asset, name) for name in _ASSET_FIELDS}, rules.overrides, known)
    context = {**(asset.ingest_context or {}), "quality_rule_version": rules.version}
    if known:
        context["quality_facts"] = known
    if (asset.quality_status, asset.quality_reasons, asset.ingest_context) == (status, reasons, context):
        return False
    asset.quality_status, asset.quality_reasons, asset.ingest_context = status, reasons, context
    asset.updated_at = now
    return True


def reevaluate(c: Container, organization_id: str, asset_id: str | None = None) -> dict[str, int | None]:
    with c.sessionmaker.begin() as db:
        if db.get(Organization, organization_id) is None:
            raise QualityRuleError("기관을 찾을 수 없습니다")
        rules = load_rules(db)
        stmt = select(Asset).where(
            Asset.organization_id == organization_id, Asset.deleted_at.is_(None),
            Asset.ingest_status.in_(_EVALUATED_STATES),
        )
        if asset_id is not None:
            stmt = stmt.where(Asset.asset_id == asset_id)
        stmt = stmt.order_by(Asset.asset_id).with_for_update().execution_options(yield_per=200)
        now = c.clock.now()
        evaluated = changed = skipped = 0
        for batch in db.scalars(stmt).partitions():
            for asset in batch:
                ranges = _known_facts(asset).get("ranges") or {}
                if any(reason.startswith("out_of_range:") and ranges.get(reason.split(":", 1)[1]) is None
                       for reason in asset.quality_reasons or []):
                    skipped += 1
                    continue
                evaluated += 1
                changed += apply_rules(asset, rules, now)
            db.flush()
        if asset_id is not None and evaluated + skipped == 0:
            raise QualityRuleError("이 기관에서 재평가할 수 있는 자산을 찾을 수 없습니다")
        summary = {"rule_version": rules.version, "evaluated": evaluated, "changed": changed,
                   "skipped_missing_facts": skipped}
        c.audit.record("ASSET_QUALITY_REEVALUATED", actor_kind="SYSTEM", actor_id="cli",
                       organization_id=organization_id, target_type="Asset" if asset_id else "Organization",
                       target_id=asset_id or organization_id, details=summary, connection=db.connection())
    return summary
