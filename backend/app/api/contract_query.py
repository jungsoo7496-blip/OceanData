"""조회 조건을 계약(openapi.yaml)의 매개변수 스키마로 검사한다 (04번 요구사항 10.2, Schemathesis M4·M8).

- 문자열 조건(열거값·ID 형식·길이·UTC 'Z' 시각)은 받은 그대로의 문자열을, 숫자·불리언은 계약 형으로 바꿔
  계약 스키마로 본다 (엔드포인트가 선언하지 않은 limit 같은 조건도 계약대로 검사된다).
  계약에 없는 조회 조건은 건드리지 않는다.
- Idempotency-Key 헤더는 계약대로 8~128자.
- 경로 값에 NUL이 있으면 그런 자원은 있을 수 없으므로 404 (DB까지 가면 500이 났다).
- 인증·호출 제한 뒤에 부른다 (인증 없는 요청은 조건과 상관없이 401).
"""

from __future__ import annotations

import math
import re
from functools import lru_cache
from typing import Any

import yaml
from fastapi import Request
from jsonschema import Draft202012Validator, FormatChecker

from app.errors import ProblemError
from app.services.contracts import BASE, contracts_dir, registry

METHODS = ("get", "post", "put", "patch", "delete")
_NON_STRING = {"integer", "number", "boolean", "array", "object"}


def _rebase(node: Any, spec: dict[str, Any]) -> Any:
    """계약의 상대 참조('./schemas/common.schema.json#/...')는 스키마 레지스트리 URI로 바꾸고,
    openapi 안 참조('#/components/schemas/...')는 그 자리에 풀어 넣는다."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/"):
            target: Any = spec
            for part in ref.removeprefix("#/").split("/"):
                target = target[part]
            return _rebase(target, spec)
        return {k: (BASE + v.removeprefix("./schemas/") if k == "$ref" and isinstance(v, str)
                    and v.startswith("./schemas/") else _rebase(v, spec)) for k, v in node.items()}
    if isinstance(node, list):
        return [_rebase(v, spec) for v in node]
    return node


def _is_string_schema(schema: dict[str, Any]) -> bool:
    if "$ref" in schema:
        resolved = registry().resolver().lookup(schema["$ref"]).contents
        return _is_string_schema(resolved)
    t = schema.get("type")
    types = set(t) if isinstance(t, list) else {t} if t else set()
    if types & _NON_STRING:
        return False
    if "enum" in schema:
        return all(isinstance(v, str) for v in schema["enum"] if v is not None)
    return True


_TRUE, _FALSE = {"true", "1", "yes", "on"}, {"false", "0", "no", "off"}  # FastAPI가 받는 불리언 표기


def _schema_type(schema: dict[str, Any]) -> str:
    """조회 매개변수 스키마의 기본 형 (string | integer | number | boolean | other)."""
    if "$ref" in schema:
        return _schema_type(registry().resolver().lookup(schema["$ref"]).contents)
    t = schema.get("type")
    types = [x for x in (t if isinstance(t, list) else [t]) if x and x != "null"]
    if not types:
        return "string" if _is_string_schema(schema) else "other"
    return types[0] if types[0] in ("string", "integer", "number", "boolean") else "other"


def _coerce(kind: str, raw: str) -> tuple[Any, bool]:
    """조회 문자열을 계약 형으로 (값, 성공 여부). 숫자·불리언도 계약 범위로 보려고 여기서 바꾼다."""
    if kind == "integer":
        return (int(raw), True) if re.fullmatch(r"[+-]?[0-9]{1,18}", raw) else (raw, False)
    if kind == "number":
        try:
            v = float(raw)
        except ValueError:
            return raw, False
        return (v, True) if math.isfinite(v) else (raw, False)
    if kind == "boolean":
        low = raw.lower()
        return (low in _TRUE, True) if low in _TRUE | _FALSE else (raw, False)
    return raw, True


@lru_cache(maxsize=1)
def _validators() -> dict[tuple[str, str], dict[str, tuple[str, Draft202012Validator]]]:
    spec = yaml.safe_load((contracts_dir() / "openapi.yaml").read_text(encoding="utf-8"))
    params = spec.get("components", {}).get("parameters", {})
    out: dict[tuple[str, str], dict[str, Draft202012Validator]] = {}
    for path, item in spec.get("paths", {}).items():
        for method in METHODS:
            op = item.get(method)
            if not op:
                continue
            found: dict[str, tuple[str, Draft202012Validator]] = {}
            for raw in (item.get("parameters") or []) + (op.get("parameters") or []):
                p = params[raw["$ref"].split("/")[-1]] if "$ref" in raw else raw
                if p.get("in") != "query" or "schema" not in p:
                    continue
                schema = _rebase(p["schema"], spec)
                kind = _schema_type(schema)
                if kind != "other":
                    found[p["name"]] = (kind, Draft202012Validator(schema, registry=registry(),
                                                                   format_checker=FormatChecker()))
            if found:
                out[(method.upper(), path)] = found
    return out


def check_request_against_contract(request: Request) -> None:
    for value in request.path_params.values():
        if "\x00" in str(value):
            raise ProblemError("NOT_FOUND")
    key = request.headers.get("idempotency-key")
    if key is not None and not (8 <= len(key) <= 128):
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/header/idempotency-key", "code": "length",
                                                        "message": "Idempotency-Key는 8~128자입니다 (계약)"}])
    route = request.scope.get("route")
    validators = _validators().get((request.method, getattr(route, "path", "")))
    if not validators:
        return
    errors: list[dict[str, str]] = []
    for name, (kind, validator) in validators.items():
        for raw in request.query_params.getlist(name):
            value, ok = _coerce(kind, raw)
            if not ok:
                errors.append({"pointer": f"/query/{name}", "code": "type",
                               "message": f"계약의 조회 조건 형({kind})이 아닙니다"})
                continue
            for e in validator.iter_errors(value):
                errors.append({"pointer": f"/query/{name}", "code": str(e.validator),
                               "message": "계약의 조회 조건 형식에 맞지 않습니다"})
                break
    if errors:
        raise ProblemError("VALIDATION_ERROR", errors=errors)
