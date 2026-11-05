"""스토리지 서버 측 암호화 확인 (08번 요구사항 1.3·1.4). S3_SSE가 켜졌는데 지원하지 않으면 기동 경고 + /readyz 표시."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.container import Container

log = logging.getLogger(__name__)
_CACHE: dict[int, tuple[float, list[str]]] = {}
TTL_S = 300


def encryption_warnings(c: Container, *, refresh: bool = False) -> list[str]:
    """경고 코드 목록 (주소·자격증명 없음). 5분 동안 결과를 재사용한다."""
    if c.settings.s3_sse == "none":
        return []
    hit = _CACHE.get(id(c))
    if hit and not refresh and time.monotonic() - hit[0] < TTL_S:
        return hit[1]
    status_fn = getattr(c.storage, "encryption_status", None)
    if status_fn is None:
        warnings = ["storage_encryption_unknown"]
    else:
        try:
            bad = [zone for zone, bucket in c.settings.buckets.items() if status_fn(bucket) != "enabled"]
        except Exception:  # noqa: BLE001 - 확인 실패도 경고로 (기동을 막지 않는다)
            bad = ["*"]
        warnings = [f"storage_encryption_unsupported:{z}" for z in bad]
    _CACHE[id(c)] = (time.monotonic(), warnings)
    return warnings


def warn_on_startup(c: Container) -> None:
    w = encryption_warnings(c, refresh=True)
    if w:
        log.warning("storage_encryption_unsupported", extra={"warnings": w, "requested": c.settings.s3_sse})
