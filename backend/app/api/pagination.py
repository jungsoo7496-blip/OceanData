"""커서 페이지네이션 (api-conventions.md). 커서는 마지막 항목의 ID(ULID라 시간순)를 감싼 값이다."""

from __future__ import annotations

import base64
from typing import Any

from sqlalchemy.orm import Session

from app.errors import ProblemError


def encode_cursor(last_id: str) -> str:
    return base64.urlsafe_b64encode(last_id.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> str:
    try:
        return base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/query/cursor", "code": "invalid",
                                                        "message": "커서 형식이 올바르지 않습니다"}]) from None


def paginate(db: Session, stmt: Any, id_col: Any, limit: int, cursor: str | None) -> tuple[list[Any], str | None]:
    if cursor:
        stmt = stmt.where(id_col > decode_cursor(cursor))
    rows = list(db.execute(stmt.order_by(id_col).limit(limit + 1)).scalars())
    if len(rows) > limit:
        rows = rows[:limit]
        return rows, encode_cursor(str(getattr(rows[-1], id_col.key)))
    return rows, None
