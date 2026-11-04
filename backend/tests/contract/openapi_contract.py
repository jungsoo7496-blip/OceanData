"""contracts/openapi.yaml 기준 응답 검증 도우미 (structure.md 6장: API 응답은 계약 시험으로 검증)."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from app.services.contracts import contracts_dir

BASE = "file:///contracts/"


@lru_cache(maxsize=1)
def _load() -> tuple[dict[str, Any], Registry]:
    root = contracts_dir()
    spec = yaml.safe_load((root / "openapi.yaml").read_text(encoding="utf-8"))
    resources = [(BASE + "openapi.yaml", Resource.from_contents(spec, default_specification=DRAFT202012))]
    for path in sorted((root / "schemas").glob("*.json")):
        resources.append((BASE + "schemas/" + path.name,
                          Resource.from_contents(json.loads(path.read_text(encoding="utf-8")),
                                                 default_specification=DRAFT202012)))
    return spec, Registry().with_resources(resources)


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def response_schema_ref(method: str, path_template: str, status: int, content_type: str) -> str:
    spec, _ = _load()
    op = spec["paths"][path_template][method.lower()]
    resp = op["responses"].get(str(status))
    assert resp is not None, f"{method} {path_template}: 계약에 {status} 응답이 없다"
    pointer = f"#/paths/{_escape(path_template)}/{method.lower()}/responses/{status}"
    if "$ref" in resp:
        pointer = resp["$ref"]
        resp = spec["components"]["responses"][pointer.rsplit("/", 1)[1]]
    ctype = next((t for t in ("application/problem+json", "application/ld+json") if content_type.startswith(t)),
                 "application/json")
    assert ctype in resp.get("content", {}), f"{method} {path_template} {status}: {ctype} 없음"
    return f"{BASE}openapi.yaml{pointer}/content/{_escape(ctype)}/schema"


def assert_matches_contract(response: Any, method: str, path_template: str) -> None:
    _, registry = _load()
    ref = response_schema_ref(method, path_template, response.status_code, response.headers["content-type"])
    validator = Draft202012Validator({"$ref": ref}, registry=registry, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(response.json()), key=lambda e: list(e.path))
    assert not errors, f"{method} {path_template} {response.status_code} 계약 위반:\n" + "\n".join(
        f"  {list(e.path)}: {e.message}" for e in errors[:10])
    assert "x-request-id" in response.headers


__all__ = ["assert_matches_contract", "Path"]
