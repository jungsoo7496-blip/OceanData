"""contracts/ 읽기 (단일 기준): 스키마 레지스트리·번들, 코드표, 계약 검증기 (03번 요구사항 2·4·8·13).

운영 업로드 검증과 사전 검증 API가 같은 함수를 쓴다 (요구사항 13.2).
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from app.domain.metadata import check_schema_version
from app.domain.normalize import TimeRuleError, UnitDictionary, check_location, parse_rfc3339

# 서버가 아는 공통 메타데이터 스키마 버전 (domain.md 8장 v1.0). 새 부버전을 받으면 여기에 더한다.
SUPPORTED_SCHEMA_VERSIONS = ("1.0.0",)
BASE = "https://contracts.marine.invalid/schemas/"  # 계층형 URI라야 상대 참조가 풀린다
CODE_TABLES = {"units": "units", "metrics": "metrics", "event-categories": "categories", "purposes": "purposes",
               "quarantine-reasons": "reasons"}


def supported_schema_versions() -> list[str]:
    return list(SUPPORTED_SCHEMA_VERSIONS)


def contracts_dir() -> Path:
    env = os.environ.get("CONTRACTS_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "contracts"
        if (cand / "openapi.yaml").exists():
            return cand
    raise FileNotFoundError("contracts 디렉터리를 찾을 수 없습니다 (CONTRACTS_DIR 설정)")


@lru_cache(maxsize=1)
def schema_files() -> dict[str, dict[str, Any]]:
    out = {}
    for path in sorted((contracts_dir() / "schemas").glob("*.schema.json")):
        out[path.name.removesuffix(".schema.json")] = json.loads(path.read_text(encoding="utf-8"))
    return out


@lru_cache(maxsize=1)
def registry() -> Registry:
    return Registry().with_resources(
        (BASE + f"{name}.schema.json", Resource.from_contents(doc, default_specification=DRAFT202012))
        for name, doc in schema_files().items())


def bundle(name: str) -> dict[str, Any]:
    """common.schema.json 참조를 풀어 단일 문서로 묶는다 (요구사항 2.2)."""
    files = schema_files()
    if name not in files:
        raise KeyError(name)
    doc = copy.deepcopy(files[name])
    common = copy.deepcopy(files["common"]["$defs"])
    own = doc.get("$defs", {})
    clash = set(own) & set(common)
    if clash and name != "common":
        raise ValueError(f"$defs 이름 충돌: {sorted(clash)}")

    def rewrite(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("common.schema.json#"):
                node = {**node, "$ref": ref.removeprefix("common.schema.json")}
            return {k: rewrite(v) for k, v in node.items()}
        if isinstance(node, list):
            return [rewrite(v) for v in node]
        return node

    doc = rewrite(doc)
    if name != "common":
        doc["$defs"] = {**common, **doc.get("$defs", {})}
    doc["$id"] = f"/v1/schemas/{name}/{SUPPORTED_SCHEMA_VERSIONS[-1]}"
    return doc


@lru_cache(maxsize=8)
def code_table(table: str) -> dict[str, Any]:
    data = json.loads((contracts_dir() / "codes" / f"{table}.json").read_text(encoding="utf-8"))
    return {"table": table, "version": str(data.get("version", "1.0.0")), "items": data.get(CODE_TABLES[table], [])}


@lru_cache(maxsize=1)
def units() -> UnitDictionary:
    return UnitDictionary(code_table("units")["items"])


# ---- 검증
@dataclass(frozen=True)
class Problem:
    pointer: str
    code: str
    message: str
    severity: str = "ERROR"

    def as_dict(self) -> dict[str, str]:
        return {"pointer": self.pointer, "code": self.code, "message": self.message, "severity": self.severity}


def _ptr(path: Any) -> str:
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in path) if path else ""


def schema_problems(name: str, doc: Any) -> list[Problem]:
    validator = Draft202012Validator({"$ref": BASE + f"{name}.schema.json"}, registry=registry(),
                                     format_checker=FormatChecker())
    out = []
    for e in sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path)):
        out.append(Problem(_ptr(e.absolute_path), f"schema.{e.validator}", e.message[:300]))
    return out


def _location_problems(doc: dict[str, Any]) -> list[Problem]:
    out = []
    loc = doc.get("location")
    if isinstance(loc, dict):
        out += [Problem(p.pointer, p.code, p.message) for p in check_location(loc)]
    return out


def _observation_problems(doc: dict[str, Any]) -> list[Problem]:
    out: list[Problem] = []
    for i, ob in enumerate(doc.get("observations") or []):
        if not isinstance(ob, dict):
            continue
        base = f"/observations/{i}"
        ts = ob.get("observed_at")
        if isinstance(ts, str):
            try:
                parse_rfc3339(ts)
            except TimeRuleError:
                out.append(Problem(base + "/observed_at", "timezone_required",
                                   "observed_at에는 시간대 오프셋이 필요합니다. 오프셋 없는 원천 시각은 "
                                   "source_timestamp로 보내면 장비 정책의 source_timezone으로 변환합니다"))
        if ob.get("value") is None and ob.get("quality_flag") not in (None, "MISSING"):
            out.append(Problem(base + "/quality_flag", "missing_flag", "결측값은 quality_flag=MISSING으로 보냅니다"))
        unit = ob.get("unit")
        if isinstance(unit, str) and units().lookup(unit) is None:
            out.append(Problem(base + "/unit", "unknown_unit", "단위 사전에 없는 단위입니다 (SUSPECT로 저장)",
                               "WARNING"))
    return out


SCHEMA_FOR_CONTRACT = {"upload-manifest": "upload-manifest", "observation-batch": "observation-batch",
                       "event": "event", "asset-metadata": "asset-metadata", "agent-heartbeat": "agent-heartbeat"}


def validate_document(contract: str, doc: Any) -> tuple[list[Problem], bool]:
    """(문제 목록, 스키마 버전 거부 여부). 스키마 버전을 먼저 확인한다 (요구사항 4.3)."""
    if not isinstance(doc, dict):
        return [Problem("", "schema.type", "문서는 JSON 객체여야 합니다")], False
    if "schema_version" in doc:  # 없으면 스키마 검증이 필수 필드 누락(400)으로 보고한다
        check = check_schema_version(doc.get("schema_version"), supported_schema_versions())
        if not check.ok:
            return [Problem("/schema_version", "UNSUPPORTED_SCHEMA_VERSION", check.reason or "")], True
    problems = schema_problems(SCHEMA_FOR_CONTRACT[contract], doc)
    problems += _location_problems(doc)
    if contract == "observation-batch":
        problems += _observation_problems(doc)
    # 같은 위치의 스키마 범위 오류와 도메인 오류가 겹치면 도메인 쪽(더 구체적인 사유)을 남긴다
    domain_ptrs = {p.pointer for p in problems if not p.code.startswith("schema.")}
    problems = [p for p in problems if not (p.code.startswith("schema.") and any(
        d.startswith(p.pointer) or p.pointer.startswith(d) for d in domain_ptrs))]
    return problems, False
