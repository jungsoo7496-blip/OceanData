"""PostGIS geometry·pgvector 컬럼 타입 (추가 의존성 없이 SQL 함수로 변환)."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func
from sqlalchemy.types import UserDefinedType


class Geometry(UserDefinedType):
    """GeoJSON dict ↔ geometry(Geometry, 4326)."""

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "geometry(Geometry, 4326)"

    def bind_expression(self, bindvalue: Any) -> Any:
        return func.ST_SetSRID(func.ST_GeomFromGeoJSON(bindvalue), 4326)

    def column_expression(self, col: Any) -> Any:
        return func.ST_AsGeoJSON(col, type_=self)

    def bind_processor(self, dialect: Any) -> Any:
        return lambda v: None if v is None else json.dumps(v)

    def result_processor(self, dialect: Any, coltype: Any) -> Any:
        return lambda v: None if v is None else json.loads(v)


class Vector(UserDefinedType):
    """pgvector. 차원은 14번 스펙(장면 임베딩 모델)에서 정한다."""

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "vector"
