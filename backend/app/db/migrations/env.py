"""Alembic 환경. 마이그레이션은 MIGRATION_DATABASE_URL(소유자 계정) 또는 DATABASE_URL로 실행한다."""

import os

from alembic import context
from sqlalchemy import create_engine

from app.db import models  # noqa: F401  (메타데이터 등록)
from app.db.base import Base


def include_object(obj, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
    # PostGIS 등 확장이 만든 테이블(spatial_ref_sys)은 관리 대상이 아니다
    return not (type_ == "table" and reflected and compare_to is None)


url = os.environ.get("MIGRATION_DATABASE_URL") or os.environ["DATABASE_URL"]
engine = create_engine(url)
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True,
                      include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()
