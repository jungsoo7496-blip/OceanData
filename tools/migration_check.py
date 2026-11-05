#!/usr/bin/env python3
"""17번 릴리스 검사: 기존 DB를 건드리지 않고 임시 DB에서 마이그레이션 왕복."""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]


def check() -> int:
    raw = os.environ.get("TEST_DATABASE_URL")
    if not raw:
        print("TEST_DATABASE_URL이 없어 마이그레이션을 검증하지 못했습니다", file=sys.stderr)
        return 1
    url = make_url(raw)
    if url.get_backend_name() != "postgresql":
        print("마이그레이션 검사는 PostgreSQL 시험 서버가 필요합니다", file=sys.stderr)
        return 1
    name = f"marine_gate_{secrets.token_hex(8)}"
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    created = False
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        created = True
        env = {**os.environ, "MIGRATION_DATABASE_URL": url.set(database=name).render_as_string(hide_password=False)}
        for operation, target in (("upgrade", "head"), ("downgrade", "base"),
                                  ("upgrade", "head"), ("downgrade", "base")):
            subprocess.run(  # noqa: S603  # nosec B603
                [sys.executable, "-m", "alembic", operation, target], cwd=ROOT / "backend",
                env=env, check=True, timeout=600)
        print("임시 DB 마이그레이션 head → base → head → base 통과")
        return 0
    finally:
        try:
            if created:
                with admin.connect() as conn:
                    conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        finally:
            admin.dispose()


if __name__ == "__main__":
    raise SystemExit(check())
