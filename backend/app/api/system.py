"""헬스체크·준비 상태·기능 상태 (01번 요구사항 3·8·11)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from app import API_VERSION
from app.api.deps import container, require
from app.container import Container
from app.metrics import REGISTRY
from app.services.contracts import supported_schema_versions

router = APIRouter()
_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="readyz")


@router.get("/healthz", tags=["system"])
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _db_ok(c: Container) -> bool:
    with c.engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return True


@router.get("/readyz", tags=["system"])
def readyz(c: Container = Depends(container)) -> JSONResponse:
    from app.services import tiers

    probes = {"db": lambda: _db_ok(c), "storage": c.storage.check, "idp": c.identity.check}
    futures = {name: _pool.submit(fn) for name, fn in probes.items()}
    # WARM 계층은 준비 상태를 바꾸지 않고 경고만 한다 (HOT 업로드·조회는 계속된다, 08번 요구사항 9)
    warm = _pool.submit(tiers.check, c, "WARM") if c.settings.tiering_enabled else None
    checks: dict[str, str] = {}
    for name, fut in futures.items():
        try:
            checks[name] = "ok" if fut.result(timeout=c.settings.readiness_timeout_s + 0.5) else "failed"
        except (FutureTimeout, Exception):  # noqa: BLE001 - 의존 시스템 실패는 모두 failed로 보고
            checks[name] = "failed"
    ready = all(v == "ok" for v in checks.values())
    # 실패한 의존 시스템 이름만 돌려준다 (주소·자격증명 없음)
    body: dict = {"status": "ready" if ready else "not_ready", "checks": checks}
    from app.services.storage_check import encryption_warnings

    try:
        warnings = encryption_warnings(c) if checks.get("storage") == "ok" else []
    except Exception:  # noqa: BLE001
        warnings = []
    if warm is not None:
        try:
            warm_ok = bool(warm.result(timeout=c.settings.readiness_timeout_s + 0.5))
        except (FutureTimeout, Exception):  # noqa: BLE001
            warm_ok = False
        if not warm_ok:
            warnings = [*warnings, "storage_tier_unavailable:WARM"]
    if warnings:  # 준비 상태는 바꾸지 않고 상세에 표시 (요구사항 1.4)
        body["warnings"] = warnings
    return JSONResponse(body, status_code=200 if ready else 503)


@router.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


@router.get("/v1/system/capabilities", tags=["system"])
def capabilities(c: Container = Depends(container), _=Depends(require("system:capabilities"))) -> dict:
    s = c.settings
    steps = [p for p in s.processing_steps if p in {"META", "PROXY", "THUMBNAIL", "SENSOR_PARSE", "DEID",
                                                     "TAG", "EMBED"}]
    return {
        "api_version": API_VERSION,
        "schema_versions": supported_schema_versions(),
        "gpu_enabled": s.gpu_enabled,
        "processing_steps": steps,
        "report_formats": [f for f in s.report_formats if f in {"PDF", "XLSX", "HWPX"}],
        "external_sources": list(s.external_sources),
        "features": {"break_glass": True, "webhooks": True, "sharing": True},
    }
