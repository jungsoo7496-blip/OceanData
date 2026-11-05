"""01번 단위 시험: 설정, 로그 마스킹, ID, 권한 표, domain import 규칙."""

from __future__ import annotations

import ast
import json
import logging
import pathlib

import pytest

from app.config import ConfigError, build_settings
from app.domain import permissions
from app.domain.ids import has_prefix, is_ulid, new_id, new_ulid
from app.domain.principal import Principal
from app.logging_setup import REDACTED, JsonFormatter, scrub, strip_query


def test_missing_settings_report_only_key_names(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("DATABASE_URL", "S3_ENDPOINT_URL", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "IDP_ISSUER_URL",
              "DEVICE_TOKEN_SIGNING_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ConfigError) as ei:
        build_settings(s3_secret_access_key="super-secret-value-123")
    msg = ei.value.message
    assert "DATABASE_URL" in msg and "IDP_ISSUER_URL" in msg
    assert "S3_SECRET_ACCESS_KEY" not in ei.value.missing
    assert "super-secret-value-123" not in msg


def test_invalid_setting_does_not_echo_value() -> None:
    with pytest.raises(ConfigError) as ei:
        build_settings(database_url="x", s3_endpoint_url="x", s3_access_key_id="a", s3_secret_access_key="b",
                       idp_issuer_url="i", device_token_signing_key="k", gpu_enabled="maybe-secret-ish")
    assert ei.value.invalid == ["GPU_ENABLED"]
    assert "maybe-secret-ish" not in ei.value.message


def test_csv_settings() -> None:
    s = build_settings(database_url="x", s3_endpoint_url="x", s3_access_key_id="a", s3_secret_access_key="b",
                       idp_issuer_url="i", device_token_signing_key="k", processing_steps="META, PROXY")
    assert s.processing_steps == ["META", "PROXY"]
    assert s.gpu_enabled is False


def test_strip_query_and_scrub() -> None:
    url = "https://s3.local/raw/org/x?X-Amz-Signature=abc&X-Amz-Credential=def#frag"
    assert strip_query(f"put {url} done") == "put https://s3.local/raw/org/x done"
    assert scrub({"client_secret": "s", "nested": {"api_key": "k", "ok": 1}}) == \
        {"client_secret": REDACTED, "nested": {"api_key": REDACTED, "ok": 1}}
    assert "eyJ" not in scrub("Authorization: Bearer eyJabc.def.ghi")


def test_json_log_has_required_fields_and_no_secrets() -> None:
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "upload to %s", ("https://h/p?sig=1",), None)
    rec.token = "tok"
    out = json.loads(JsonFormatter().format(rec))
    assert {"timestamp", "level", "request_id", "organization_id", "event"} <= out.keys()
    assert out["event"] == "upload to https://h/p"
    assert out["token"] == REDACTED


def test_json_log_drops_db_error_values_but_keeps_sqlstate() -> None:
    """DB 오류 문구·바인딩 값에 든 개인정보(합성)가 처리되지 않은 예외 로그에 남지 않는다 (security.md)."""
    import sys

    import pg8000.dbapi
    from sqlalchemy.exc import DataError

    from app.db.session import make_engine

    pii = "홍길동 010-0000-0000"  # 합성 값 (traceback의 소스 줄에는 나오지 않게 변수로)
    orig = pg8000.dbapi.DatabaseError({"S": "ERROR", "C": "22P02", "M": f'invalid input syntax for type integer: "{pii}"'})
    stmt, params = "SELECT CAST(%s AS integer)", (pii,)
    for chained in (False, True):
        try:
            try:
                raise DataError(stmt, params, orig)
            except DataError as db_exc:
                if chained:
                    raise RuntimeError("보고서 만들기 실패") from db_exc
                raise
        except Exception:  # noqa: BLE001 - 로그 형식만 본다
            rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "unhandled_exception", None, sys.exc_info())
        out = json.loads(JsonFormatter().format(rec))
        assert "홍길동" not in out["exc"] and "010-0000-0000" not in out["exc"], out["exc"]
        assert "SQLSTATE=22P02" in out["exc"] and "Traceback" in out["exc"]
    assert make_engine("postgresql+pg8000://u:p@127.0.0.1:1/x").hide_parameters is True


def test_ulid_and_prefix() -> None:
    a, b = new_ulid(), new_ulid()
    assert is_ulid(a) and len(a) == 26 and a != b
    assert new_ulid(1000) < new_ulid(2000)
    assert has_prefix(new_id("asset"), "asset") and not has_prefix("ast_123", "asset")
    assert not is_ulid("01ARZ3NDEKTSV4RRFFQ69G5FAI")  # I는 Crockford에 없다


def _p(roles: set[str], kind: str = "USER", mfa: bool = False, scopes: set[str] | None = None) -> Principal:
    return Principal(kind=kind, organization_id="org_x", org_type="PROVIDER", roles=frozenset(roles), mfa=mfa,
                     scopes=frozenset(scopes or ()))


def test_permission_default_deny_and_mfa() -> None:
    with pytest.raises(permissions.PermissionDenied):
        permissions.check(_p({"ORG_ADMIN"}), "no:such:action")
    with pytest.raises(permissions.PermissionDenied) as ei:
        permissions.check(_p({"ORG_ADMIN"}), "api_key:create")
    assert ei.value.mfa_required
    permissions.check(_p({"ORG_ADMIN"}, mfa=True), "api_key:create")
    with pytest.raises(permissions.PermissionDenied) as ei:
        permissions.check(_p({"ORG_MEMBER"}, mfa=True), "api_key:create")
    assert not ei.value.mfa_required
    # 기기는 범위가 있어야 한다
    permissions.check(_p(set(), kind="DEVICE", scopes={"upload:write"}), "upload:write")
    with pytest.raises(permissions.PermissionDenied):
        permissions.check(_p(set(), kind="DEVICE", scopes={"upload:write"}), "api_key:create")


def test_every_permission_role_is_a_domain_role() -> None:
    roles = {"ORG_ADMIN", "ORG_MEMBER", "PROGRAM_VIEWER", "PLATFORM_OPERATOR", "REVIEWER", "CONSUMER"}
    for action, rule in permissions.PERMISSIONS.items():
        assert rule.roles <= roles, action


FORBIDDEN_IMPORTS = ("fastapi", "starlette", "sqlalchemy", "boto3", "botocore", "httpx", "requests", "pydantic",
                     "jwt", "alembic", "app.db", "app.api", "app.adapters", "app.services")


def test_domain_layer_has_no_framework_imports() -> None:
    root = pathlib.Path(__file__).resolve().parents[2] / "app" / "domain"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            for n in names:
                assert not n.startswith(FORBIDDEN_IMPORTS), f"{path.name}: {n}"
