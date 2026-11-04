"""공통 메타데이터 규칙 (domain.md 3·6·8장, 03번 요구사항 3·4). 순수 함수만."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULTS = {
    "data_role": "CORE",
    "privacy_status": "REVIEW_REQUIRED",
    "sensitivity_class": "RESTRICTED",
    "sharing_scope": "PRIVATE",
    "share_status": "NONE",
    "backup_class": "NONE",
    "quality_status": "PENDING",
}

# collection_mode별로 센터가 받는 영상·이미지 variant (domain.md 3장 표)
MEDIA_VARIANTS_BY_MODE = {
    "C0_METADATA_ONLY": frozenset(),
    "C1_SAMPLE_PREVIEW": frozenset({"SAMPLE_FRAME", "PROXY"}),
    "C2_EVENT_CLIPS": frozenset({"SAMPLE_FRAME", "PROXY", "CLIP"}),
    "C3_APPROVED_FULL": frozenset({"SAMPLE_FRAME", "PROXY", "CLIP", "ORIGINAL"}),
}
MEDIA_TYPES = frozenset({"VIDEO", "IMAGE"})
STRICTNESS_ROLE = {"CORE": 2, "BYPRODUCT": 1}
STRICTNESS_SENSITIVITY = {"SECURITY": 3, "RESTRICTED": 2, "INTERNAL": 1}


def variant_allowed(collection_mode: str, data_type: str, variant: str) -> bool:
    """센터가 이 variant의 바이너리를 받을 수 있는가. 허용되지 않으면 422 RULE_VIOLATION."""
    if collection_mode == "C0_METADATA_ONLY":
        return False
    if data_type in MEDIA_TYPES:
        return variant in MEDIA_VARIANTS_BY_MODE[collection_mode]
    return True  # 그 밖의 유형은 C1 이상이면 ORIGINAL 포함 허용


def inherit_strictest(parents: list[dict[str, str]]) -> dict[str, str]:
    """파생 자산은 원 자산들의 가장 엄격한 data_role·sensitivity_class를 물려받는다."""
    if not parents:
        return {}
    role = max((p["data_role"] for p in parents), key=STRICTNESS_ROLE.__getitem__)
    sens = max((p["sensitivity_class"] for p in parents), key=STRICTNESS_SENSITIVITY.__getitem__)
    return {"data_role": role, "sensitivity_class": sens}


def apply_defaults(fields: dict, *, device_collection_mode: str, rule_data_role: str | None = None) -> dict:
    """기업이 입력하지 않은 값에 안전한 기본값을 넣는다 (03번 요구사항 3.1). 들어온 값은 덮어쓰지 않는다."""
    out = dict(fields)
    out.setdefault("collection_mode", device_collection_mode)
    if "data_role" not in out or out["data_role"] is None:
        out["data_role"] = rule_data_role or DEFAULTS["data_role"]
    for k in ("privacy_status", "sensitivity_class", "sharing_scope", "share_status", "backup_class",
              "quality_status"):
        if out.get(k) is None:
            out[k] = DEFAULTS[k]
    return out


@dataclass(frozen=True)
class SchemaVersionCheck:
    ok: bool
    reason: str | None = None


def check_schema_version(declared: object, supported: list[str]) -> SchemaVersionCheck:
    """주버전이 다르거나 서버가 모르는 더 높은 부버전이면 거부, 같거나 낮은 부버전은 받는다 (03번 요구사항 4.3)."""
    if not isinstance(declared, str):
        return SchemaVersionCheck(False, "schema_version이 없습니다")
    try:
        major, minor, _patch = (int(x) for x in declared.split("."))
    except ValueError:
        return SchemaVersionCheck(False, "schema_version 형식은 주.부.수입니다")
    known = [tuple(int(x) for x in v.split(".")) for v in supported]
    same_major = [v for v in known if v[0] == major]
    if not same_major:
        return SchemaVersionCheck(False, f"주버전 {major}은 지원하지 않습니다")
    if minor > max(v[1] for v in same_major):
        return SchemaVersionCheck(False, f"부버전 {major}.{minor}은 서버가 아직 모릅니다")
    return SchemaVersionCheck(True)
