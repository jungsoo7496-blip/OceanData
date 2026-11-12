from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(url: str, *, pool_size: int = 10, max_overflow: int = 20, pool_timeout: float = 30.0) -> Engine:
    # hide_parameters: 처리되지 않은 DB 오류가 로그에 바인딩 값(개인정보일 수 있다)을 남기지 않게 한다 (security.md)
    # 풀 크기는 설정(DB_POOL_*)으로 정한다. 요청이 동시에 쓰는 양은 요청 슬롯이 묶는다 (ADR-0062)
    return create_engine(url, pool_pre_ping=True, pool_size=pool_size, max_overflow=max_overflow,
                         pool_timeout=pool_timeout, future=True, hide_parameters=True)


@lru_cache(maxsize=4)
def cached_engine(url: str) -> Engine:
    return make_engine(url)


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
