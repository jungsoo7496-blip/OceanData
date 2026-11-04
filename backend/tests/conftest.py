"""공통 픽스처. 통합 시험은 TEST_DATABASE_URL(소유자 계정, 기존 DB 주소)이 있어야 돈다.

시험마다 새 DB를 만들어 마이그레이션을 적용하고, 끝나면 지운다.
"""

from __future__ import annotations

import os
import secrets
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

from app.adapters.keycloak_identity import KeycloakIdentity
from app.adapters.notifier import LogNotifier
from app.config import Settings, build_settings

ISSUER = "http://idp.test/realms/marine"
AUDIENCE = "marine-api"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_JWK = {**jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key(), as_dict=True), "kid": "test-kid", "use": "sig",
        "alg": "RS256"}


def user_token(subject: str, *, mfa: bool = False, org: str | None = None, exp_s: int = 300,
               aud: str = AUDIENCE, iss: str = ISSUER, key: Any = None) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {"iss": iss, "aud": aud, "sub": subject, "iat": now, "exp": now + exp_s,
                              "preferred_username": "test", "realm_access": {"roles": ["default-roles-marine"]}}
    if mfa:
        claims["amr"] = ["pwd", "otp"]
    if org:
        claims["organization_id"] = org
    return jwt.encode(claims, key or _KEY, algorithm="RS256", headers={"kid": "test-kid"})


class FakeStorage:
    def __init__(self) -> None:
        self.ok = True

    def check(self) -> bool:
        return self.ok


class StaticIdentity(KeycloakIdentity):
    def __init__(self) -> None:
        super().__init__(ISSUER, AUDIENCE, jwks_provider=lambda: {"keys": [_JWK]})
        self.ok = True

    def check(self) -> bool:
        return self.ok


def test_settings(database_url: str = "postgresql+pg8000://u:p@127.0.0.1:1/x", **kw: Any) -> Settings:
    base: dict[str, Any] = dict(
        database_url=database_url, s3_endpoint_url="http://s3.test:8333", s3_access_key_id="test-access",
        s3_secret_access_key="test-secret-value", idp_issuer_url=ISSUER,
        device_token_signing_key="test-signing-key-" + "x" * 32, readiness_timeout_s=0.5,
    )
    base.update(kw)
    return build_settings(**base)


@pytest.fixture(scope="session")
def pg_owner_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL이 없어 통합 시험을 건너뜁니다")
    return url


@pytest.fixture()
def db_url(pg_owner_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    base = make_url(pg_owner_url)
    name = f"marine_test_{secrets.token_hex(4)}"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.set(database=name).render_as_string(hide_password=False)
    from alembic import command
    from alembic.config import Config

    monkeypatch.setenv("MIGRATION_DATABASE_URL", url)
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "app/db/migrations"))
    command.upgrade(cfg, "head")
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture()
def engine(db_url: str) -> Iterator[Engine]:
    from app.db.session import make_engine

    e = make_engine(db_url)
    yield e
    e.dispose()


class Ctx:
    def __init__(self, client: Any, container: Any) -> None:
        self.client = client
        self.c = container

    def as_user(self, n: int, *, mfa: bool = False) -> dict[str, str]:
        from app.seed import sub

        return {"Authorization": f"Bearer {user_token(sub(n), mfa=mfa)}"}

    def audit_actions(self) -> list[str]:
        with self.c.engine.connect() as conn:
            return [r[0] for r in conn.execute(text("SELECT action FROM audit_log ORDER BY seq"))]


@pytest.fixture()
def ctx(db_url: str, engine: Engine) -> Iterator[Ctx]:
    from fastapi.testclient import TestClient

    from app.container import build_container
    from app.main import create_app
    from app.seed import seed

    settings = test_settings(db_url)
    container = build_container(settings, engine=engine, storage=FakeStorage(), identity=StaticIdentity(),
                                notifier=LogNotifier())
    with container.sessionmaker.begin() as db:
        seed(db)
    app = create_app(container)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield Ctx(client, container)


def utcnow() -> datetime:
    return datetime.now(UTC)


def in_future(**kw: float) -> str:
    return (utcnow() + timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- 실제 S3 API가 필요한 시험 (04번): TEST_S3_ENDPOINT(SeaweedFS 등)가 있으면 그것, 없으면 moto 서버
@pytest.fixture(scope="session")
def s3_endpoint() -> Iterator[dict[str, str]]:
    endpoint = os.environ.get("TEST_S3_ENDPOINT")
    if endpoint:
        yield {"url": endpoint, "key": os.environ.get("TEST_S3_ACCESS_KEY", "dev-access"),
               "secret": os.environ.get("TEST_S3_SECRET_KEY", "change-me-s3")}
        return
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield {"url": f"http://{host}:{port}", "key": "test-access", "secret": "test-secret-value"}
    server.stop()


@pytest.fixture()
def ictx(db_url: str, engine: Engine, s3_endpoint: dict[str, str]) -> Iterator[Ctx]:
    from fastapi.testclient import TestClient

    from app.adapters.s3_storage import S3Storage
    from app.container import build_container
    from app.main import create_app
    from app.seed import seed

    suffix = secrets.token_hex(3)
    settings = test_settings(
        db_url, s3_endpoint_url=s3_endpoint["url"], s3_access_key_id=s3_endpoint["key"],
        s3_secret_access_key=s3_endpoint["secret"], upload_part_size=5 * 1024 * 1024,
        report_formats=["XLSX", "HWPX", "PDF"],
        **{f"bucket_{z}": f"t{suffix}-{z}" for z in ("raw", "proc", "evidence", "datasets", "quarantine")})
    storage = S3Storage(settings)
    for b in settings.buckets.values():
        storage.ensure_bucket(b)
    container = build_container(settings, engine=engine, storage=storage, identity=StaticIdentity(),
                                notifier=LogNotifier())
    with container.sessionmaker.begin() as db:
        seed(db)
    with TestClient(create_app(container), raise_server_exceptions=False) as client:
        yield Ctx(client, container)


# ---- 17번 수용시험 보고: ACCEPTANCE_RESULTS 경로가 있으면 covers 마커가 있는 시험의 결과를 JSON Lines로 남긴다
_RESULTS = os.environ.get("ACCEPTANCE_RESULTS")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: Any, call: Any) -> Iterator[None]:
    outcome = yield
    rep = outcome.get_result()
    ids = [i for m in item.iter_markers("covers") for i in m.args]
    if not _RESULTS or not ids or (rep.when != "call" and not (rep.when == "setup" and rep.outcome != "passed")):
        return
    import json

    line = {"nodeid": item.nodeid, "covers": ids, "outcome": rep.outcome,
            "log": (rep.longreprtext or "")[-2000:] if rep.outcome == "failed" else ""}
    with open(_RESULTS, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
