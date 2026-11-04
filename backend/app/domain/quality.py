"""Quality_Evaluator — 설정 가능한 규칙으로 quality_status와 사유를 계산한다 (03번 요구사항 10)."""

from __future__ import annotations

from typing import Any

DEFAULT_RULES: dict[str, Any] = {
    "version": 1,
    "required_fields": {"VIDEO": ["mime_type", "byte_size", "checksum_sha256"],
                        "SENSOR": ["mime_type", "byte_size", "checksum_sha256"]},
    "capture_time_unknown": "WARNING",
    "unknown_unit": "WARNING",
    "not_playable": "FAILED",
    "value_ranges": {},
}
_ORDER = {"PASSED": 0, "WARNING": 1, "FAILED": 2}


def evaluate(asset: dict[str, Any], rules: dict[str, Any] | None = None,
             facts: dict[str, Any] | None = None) -> tuple[str, list[str]]:
    """asset: 메타데이터 필드, facts: 처리 단계가 알아낸 사실(playable, unknown_units, ranges 등)."""
    r = {**DEFAULT_RULES, **(rules or {})}
    f = facts or {}
    reasons: list[tuple[str, str]] = []
    for name in r["required_fields"].get(asset.get("data_type", ""), []):
        if asset.get(name) in (None, ""):
            reasons.append(("FAILED", f"missing:{name}"))
    if (asset.get("timestamp_quality") or {}).get("source", "UNKNOWN") == "UNKNOWN":
        reasons.append((r["capture_time_unknown"], "capture_time_unknown"))
    if f.get("unknown_units"):
        reasons.append((r["unknown_unit"], "unknown_unit"))
    if f.get("playable") is False:
        reasons.append((r["not_playable"], "not_playable"))
    for metric, (lo, hi) in (r.get("value_ranges") or {}).items():
        v = (f.get("ranges") or {}).get(metric)
        if v is not None and not (lo <= v <= hi):
            reasons.append(("WARNING", f"out_of_range:{metric}"))
    if not reasons:
        return "PASSED", []
    status = max((s for s, _ in reasons), key=_ORDER.__getitem__)
    return status, sorted({why for _, why in reasons})
