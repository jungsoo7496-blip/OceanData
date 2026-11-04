from __future__ import annotations

from datetime import UTC, datetime


def iso(dt: datetime | None) -> str | None:
    """RFC 3339 UTC(Z). 저장값이 tz 없는 시각이면 UTC로 본다."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
