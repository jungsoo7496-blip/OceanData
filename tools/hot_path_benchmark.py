#!/usr/bin/env python3
"""Measure migration 0024 on a disposable, synthetic PostgreSQL database."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import URL, Engine, make_url
from starlette.requests import Request

from app.adapters.system_clock import FixedClock
from app.api.assets import list_assets
from app.api.observations import list_observations
from app.container import Container, build_container
from app.domain.principal import Principal
from app.services import monitoring
from app.services.ingest import IngestService
from app.workers.main import HANDLERS
from tests.conftest import test_settings

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 5, 9, tzinfo=UTC)
ORG = f"org_{1:026d}"
SESSION = f"ses_{1192:026d}"
PRINCIPAL = Principal("USER", ORG, "ORG_PROVIDER", roles=frozenset({"ORG_ADMIN"}))
PAGE = {"limit": 500, "cursor": None}
BURST_START = f"job_{1_088_157:026d}"
SAMPLES = 7
Statement = tuple[str, Any]


@contextmanager
def capture(engine: Engine, *, stop_at: str | None = None) -> Iterator[list[Statement]]:
    seen: list[Statement] = []

    class Captured(Exception):
        pass

    def before_execute(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool,
    ) -> None:
        seen.append((statement, parameters))
        if stop_at and stop_at in statement:
            raise Captured

    event.listen(engine, "before_cursor_execute", before_execute)
    try:
        yield seen
    except Captured:
        pass
    finally:
        event.remove(engine, "before_cursor_execute", before_execute)


def matching(seen: list[Statement], *needles: str) -> list[Statement]:
    result = [(query, params) for query, params in seen if all(part in query for part in needles)]
    if not result:
        raise RuntimeError(f"Application statement missing: {needles}")
    return result


def statements(c: Container) -> dict[str, Statement]:
    queries: dict[str, Statement] = {}
    with capture(c.engine, stop_at="FOR UPDATE OF j SKIP LOCKED") as seen:
        c.queue.claim(list(HANDLERS), "benchmark", c.settings.worker_resource_classes)
    queries["claim_burst"] = matching(seen, "FOR UPDATE OF j SKIP LOCKED")[0]

    with capture(c.engine, stop_at="FOR UPDATE SKIP LOCKED") as seen, c.sessionmaker() as db:
        IngestService(c, db).sweep_expired()
    queries["expired_uploads"] = matching(seen, "FROM uploads", "FOR UPDATE SKIP LOCKED")[0]

    rules: list[tuple[str, Callable[..., Any], tuple[str, ...], tuple[str, ...]]] = [
        ("STORAGE_FORECAST", monitoring._storage_forecast, ("sum(assets.byte_size)",), ("storage_forecast",)),
        ("VOLUME_DROP", monitoring._volume_drop, ("FROM assets", "assets.variant ="), ("volume_day", "volume_base")),
        ("QUARANTINE_SURGE", monitoring._quarantine_surge, ("FROM assets", "assets.ingest_status ="),
         ("quarantine_hour", "quarantine_base")),
        ("UPLOAD_FAILURE_RATE", monitoring._upload_failure_rate, ("FROM uploads", "uploads.created_at >="),
         ("upload_failure_rate",)),
    ]
    with c.sessionmaker() as db:
        ctx = monitoring.EvalCtx(db, NOW, c.settings)
        for code, evaluate, match, labels in rules:
            params = monitoring.rule_params(db, code)
            with capture(c.engine) as seen:
                list(evaluate(ctx, params))
            queries.update(zip(labels, matching(seen, *match), strict=True))
        params = monitoring.rule_params(db, "SESSION_NO_DATA")
        with capture(c.engine) as seen:
            list(monitoring._session_no_data(ctx, params))
        for number, query in enumerate(matching(seen, "SELECT EXISTS", "assets.ingested_at"), 1):
            queries[f"session_no_data_{number}"] = query
        with capture(c.engine) as seen:
            monitoring.top_failure_reasons(db, NOW, ORG)
        queries["upload_failure_reasons"] = matching(seen, "FROM uploads", "uploads.updated_at >=")[0]
        queries["asset_failure_reasons"] = matching(seen, "FROM assets", "assets.ingested_at >=")[0]

        for status, limit in (("REGISTERED", 500), ("QUARANTINED", 20)):
            with capture(c.engine) as seen:
                list_assets(Request({"type": "http"}), ingest_status=status, db=db, p=PRINCIPAL,
                            page={**PAGE, "limit": limit})
            queries[f"assets_{status.lower()}"] = matching(seen, "FROM assets", "assets.ingest_status =", "LIMIT")[0]

        for label, window, session in (
            ("observations_day", timedelta(days=1), None),
            ("observations_week", timedelta(days=7), None),
            ("observations_session", timedelta(hours=12), SESSION),
        ):
            with capture(c.engine) as seen:
                list_observations(session_id=session, from_=NOW - window, to=NOW, interval=None,
                                  db=db, p=PRINCIPAL, page=PAGE)
            queries[label] = matching(seen, "FROM observations")[0]
    return queries


def index_names(plan: dict[str, Any]) -> set[str]:
    names = {plan["Index Name"]} if "Index Name" in plan else set()
    for child in plan.get("Plans", []):
        names.update(index_names(child))
    return names


def explain(cursor: Any, statement: Statement) -> dict[str, Any]:
    query, params = statement
    samples = []
    indexes: set[str] = set()
    for sample in range(SAMPLES + 1):
        cursor.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query, params)
        value = cursor.fetchone()[0]
        result = (json.loads(value) if isinstance(value, str) else value)[0]
        if sample:
            plan = result["Plan"]
            buffers = plan.get("Shared Hit Blocks", 0) + plan.get("Shared Read Blocks", 0)
            samples.append((result["Execution Time"], buffers))
            indexes.update(index_names(plan))
    return {"ms": statistics.median(ms for ms, _ in samples),
            "buffers": statistics.median(blocks for _, blocks in samples), "indexes": sorted(indexes)}


def measure(engine: Engine, queries: dict[str, Statement]) -> dict[str, dict[str, Any]]:
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        measured = {name: explain(cursor, statement) for name, statement in queries.items()}
        raw.rollback()
        cursor.execute("""
            UPDATE assets SET ingest_status = 'REGISTERED'
            WHERE asset_id IN (
                SELECT asset_id FROM assets
                WHERE organization_id = %s AND ingest_status = 'READY_PRIVATE' AND deleted_at IS NULL
                ORDER BY asset_id DESC LIMIT 9181
            )
        """, (ORG,))
        cursor.execute("ANALYZE assets")
        measured["assets_registered_burst"] = explain(cursor, queries["assets_registered"])
        return measured
    finally:
        raw.rollback()
        raw.close()


def measure_claim(engine: Engine, statement: Statement) -> dict[str, Any]:
    raw = engine.raw_connection()
    try:
        return explain(raw.cursor(), statement)
    finally:
        raw.rollback()
        raw.close()


def prepare_quiet_jobs(engine: Engine) -> None:
    # 대기 행을 커밋 후 VACUUM하여 이미 지운 행의 인덱스 항목이 평시 측정에 섞이지 않게 한다.
    with engine.begin() as conn:
        deleted = conn.execute(text("DELETE FROM jobs WHERE status = 'QUEUED' AND job_id >= :start"),
                               {"start": BURST_START})
        if deleted.rowcount != 40_000:
            raise RuntimeError(f"Expected 40000 synthetic burst jobs, deleted {deleted.rowcount}")
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("VACUUM (ANALYZE, INDEX_CLEANUP ON) jobs"))


def migrate(url: URL, command: str, revision: str) -> float:
    env = {**os.environ, "MIGRATION_DATABASE_URL": url.render_as_string(hide_password=False)}
    started = time.perf_counter()
    subprocess.run(  # noqa: S603 - 고정된 실행 파일과 내부에서 지정한 Alembic 인수
        [sys.executable, "-m", "alembic", command, revision], cwd=ROOT / "backend", env=env, check=True, timeout=600,
    )
    return time.perf_counter() - started


def seed(url: URL) -> None:
    psql = shutil.which("psql") or "/usr/lib/postgresql/16/bin/psql"
    env = {**os.environ, "PGHOST": url.host or "", "PGPORT": str(url.port or 5432),
           "PGUSER": url.username or "", "PGPASSWORD": url.password or ""}
    subprocess.run(  # noqa: S603 - 고정 스크립트를 새로 만든 합성 DB에 실행
        [psql, "-X", "-v", "ON_ERROR_STOP=1", "-d", str(url.database), "-f",
         str(ROOT / "tools/hot_path_benchmark_seed.sql")], env=env, check=True, timeout=1800,
    )


def counts(engine: Engine) -> dict[str, int]:
    tables = ("assets", "jobs", "uploads", "observations", "audit_log", "events", "tier_moves")
    with engine.connect() as conn:
        return {
            table: int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())  # noqa: S608 - 고정 표 목록
            for table in tables
        }


def index_catalog(engine: Engine) -> dict[str, str]:
    with engine.connect() as conn:
        return dict(conn.execute(text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'")).all())


def run() -> None:
    base = make_url(os.environ["TEST_DATABASE_URL"])
    if base.get_backend_name() != "postgresql":
        raise ValueError("TEST_DATABASE_URL must point to the PostgreSQL test server")
    name = f"marine_perf_{secrets.token_hex(8)}"
    url = base.set(database=name)
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    engine = create_engine(url)
    created = False
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        created = True
        print(f"Synthetic database created: {name}", flush=True)
        migrate(url, "upgrade", "0023")
        seed(url)
        settings = test_settings(url.render_as_string(hide_password=False))
        c = build_container(settings, engine=engine, clock=FixedClock(NOW))
        row_counts = counts(engine)
        original_indexes = index_catalog(engine)
        with engine.connect() as conn:
            stats = dict(conn.execute(text("""
                SELECT name, setting || coalesce(unit, '') FROM pg_settings
                WHERE name IN ('server_version', 'shared_buffers', 'work_mem',
                               'random_page_cost', 'max_parallel_workers_per_gather')
            """)).all())
        print(json.dumps({"synthetic_counts": row_counts, "settings": stats}), flush=True)
        queries = statements(c)
        before = measure(engine, queries)
        upgrade_s = migrate(url, "upgrade", "0024")
        with engine.begin() as conn:
            conn.execute(text("ANALYZE jobs, assets"))
        after = measure(engine, queries)
        downgrade_s = migrate(url, "downgrade", "0023")
        if counts(engine) != row_counts or index_catalog(engine) != original_indexes:
            raise RuntimeError("Migration round trip changed synthetic row counts or the 0023 indexes")
        print("0023 → 0024 → 0023 populated database counts and indexes match", flush=True)
        prepare_quiet_jobs(engine)
        before["claim_quiet"] = measure_claim(engine, queries["claim_burst"])
        migrate(url, "upgrade", "0024")
        after["claim_quiet"] = measure_claim(engine, queries["claim_burst"])
        print(json.dumps({"samples": SAMPLES, "migration_s": {"upgrade": upgrade_s, "downgrade": downgrade_s},
                          "before": before, "after": after}, indent=2), flush=True)
    finally:
        engine.dispose()
        try:
            if created:
                with admin.connect() as conn:
                    conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
                print(f"Synthetic database dropped: {name}", flush=True)
        finally:
            admin.dispose()


if __name__ == "__main__":
    run()
