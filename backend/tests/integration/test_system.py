"""01번 통합 시험: 헬스체크, 오류 형식, 요청 ID, 메트릭, 기능 상태."""

from __future__ import annotations

import pytest

from app.domain.ids import is_ulid

pytestmark = pytest.mark.integration


def test_healthz_and_request_id(ctx) -> None:
    r = ctx.client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert is_ulid(r.headers["x-request-id"])
    rid = "01J0000000000000000000000Z"
    assert ctx.client.get("/healthz", headers={"X-Request-Id": rid}).headers["x-request-id"] == rid
    # 형식이 틀린 요청 ID는 새로 만든다 (로그 주입 방지)
    assert ctx.client.get("/healthz", headers={"X-Request-Id": "bad\nid"}).headers["x-request-id"] != "bad\nid"


def test_readyz_reports_failed_dependency_names_only(ctx) -> None:
    r = ctx.client.get("/readyz")
    assert r.status_code == 200 and r.json() == {"status": "ready", "checks": {"db": "ok", "storage": "ok",
                                                                               "idp": "ok"}}
    ctx.c.storage.ok = False
    ctx.c.identity.ok = False
    r = ctx.client.get("/readyz")
    assert r.status_code == 503
    assert r.json() == {"status": "not_ready", "checks": {"db": "ok", "storage": "failed", "idp": "failed"}}
    assert "s3.test" not in r.text and "idp.test" not in r.text


def test_problem_format_for_404_and_unhandled(ctx) -> None:
    r = ctx.client.get("/v1/does-not-exist")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["code"] == "NOT_FOUND" and body["request_id"] == r.headers["x-request-id"]

    @ctx.client.app.get("/__boom")
    def boom() -> None:
        raise RuntimeError("SELECT secret FROM /internal/path")

    r = ctx.client.get("/__boom")
    assert r.status_code == 500 and r.json()["code"] == "INTERNAL"
    assert "internal/path" not in r.text and "SELECT" not in r.text


def test_validation_error_has_json_pointers(ctx) -> None:
    r = ctx.client.post("/v1/sessions", json={"project_id": "bad", "title": ""}, headers=ctx.as_user(102))
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "VALIDATION_ERROR"
    pointers = {e["pointer"] for e in body["errors"]}
    assert {"/project_id", "/label", "/title", "/starts_at"} <= pointers


def test_metrics_exposed(ctx) -> None:
    ctx.client.get("/healthz")
    r = ctx.client.get("/metrics")
    assert r.status_code == 200
    assert 'http_requests_total{method="GET",route="/healthz",status="200"}' in r.text
    assert "http_request_duration_seconds" in r.text


def test_capabilities_requires_auth_and_hides_settings(ctx) -> None:
    assert ctx.client.get("/v1/system/capabilities").status_code == 401
    r = ctx.client.get("/v1/system/capabilities", headers=ctx.as_user(102))
    assert r.status_code == 200
    body = r.json()
    assert body["gpu_enabled"] is False and body["schema_versions"] == ["1.0.0"]
    assert body["processing_steps"] == ["META"]
    for secret in ("s3.test", "test-secret-value", "raw", "pg8000", "idp.test"):
        assert secret not in r.text
