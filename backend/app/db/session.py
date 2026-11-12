from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(url: str) -> Engine:
    # hide_parameters: 처리되지 않은 DB 오류가 로그에 바인딩 값(개인정보일 수 있다)을 남기지 않게 한다 (security.md)
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=20, future=True, hide_parameters=True)


@lru_cache(maxsize=4)
def cached_engine(url: str) -> Engine:
    return make_engine(url)


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
