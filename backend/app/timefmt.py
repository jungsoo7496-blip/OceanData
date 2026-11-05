from __future__ import annotations

from datetime import UTC, datetime


def iso(dt: datetime | None) -> str | None:
    """RFC 3339 UTC(Z). 저장값이 tz 없는 시각이면 UTC로 본다."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    d = dt.astimezone(UTC)  # strftime("%Y")는 1000년 이전 연도를 0으로 채우지 않는다 ("63-05-29…") — 직접 4자리로
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}T{d.hour:02d}:{d.minute:02d}:{d.second:02d}Z"
