"""contracts/ 읽기 (단일 기준): 스키마 레지스트리·번들, 코드표, 계약 검증기 (03번 요구사항 2·4·8·13).

운영 업로드 검증과 사전 검증 API가 같은 함수를 쓴다 (요구사항 13.2).
"""

from __future__ import annotations

import copy
import itertools
import json
import math
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker, validators
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from app.domain.metadata import check_schema_version
from app.domain.normalize import (
    NoOffsetError,
    TimeRangeError,
    TimeRuleError,
    UnitDictionary,
    check_location,
    parse_rfc3339,
)

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


def _ecma_anchors(pattern: str) -> str:
    """Python의 `$`는 끝의 개행 앞에도 맞는다(`"abc\\n"`이 `^abc$`를 통과).

    계약 패턴은 ECMA-262 문법이라 `$`는 입력 끝에만 맞아야 하므로 검증용 사본에서만 `\\Z`로 바꾼다.
    이스케이프(`\\$`)와 문자 클래스(`[$]`) 안은 그대로 둔다."""
    out: list[str] = []
    in_class, i = False, 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            out.append(pattern[i:i + 2])
            i += 2
            continue
        if in_class:
            in_class = ch != "]"
        elif ch == "[":
            in_class = True
        elif ch == "$":
            out.append("\\Z")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def strict_patterns(node: Any) -> Any:
    """스키마 사본에서 pattern·patternProperties의 `$`를 입력 끝으로 고친다. 내려주는 스키마는 바꾸지 않는다."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k == "pattern" and isinstance(v, str):
                out[k] = _ecma_anchors(v)
            elif k == "patternProperties" and isinstance(v, dict):
                out[k] = {_ecma_anchors(pk): strict_patterns(pv) for pk, pv in v.items()}
            else:
                out[k] = strict_patterns(v)
        return out
    if isinstance(node, list):
        return [strict_patterns(v) for v in node]
    return node


def _validation_copy(doc: dict[str, Any]) -> dict[str, Any]:
    """검증에 쓰는 스키마 사본: 끝 `$` 고침 + `$schema` 제거.

    jsonschema는 $ref로 내려갈 때 하위 문서의 `$schema`를 보고 표준 검증기 클래스로 바꿔 버려서, 우리 검증기의
    값 검사(유한한 수)가 참조된 스키마 안에서는 꺼진다. `$schema`가 없으면 부모 클래스를 그대로 쓴다.
    규격은 Resource의 default_specification이 정한다."""
    out: dict[str, Any] = strict_patterns(doc)
    out.pop("$schema", None)
    return out


@lru_cache(maxsize=1)
def registry() -> Registry:
    return Registry().with_resources(
        (BASE + f"{name}.schema.json", Resource.from_contents(_validation_copy(doc), default_specification=DRAFT202012))
        for name, doc in schema_files().items())


# ---- 값 검사: 계약이 정한 값만 통과시킨다 (운영 이미지와 개발 환경이 같게)
MAX_PROBLEMS = 100  # 응답에 담는 문제 수 상한. 넘으면 앞의 100개 뒤에 schema.truncated 경고 하나를 붙인다
MAX_DEPTH = 64  # 문서 중첩 상한 (계약 문서의 실제 깊이는 한 자리 수)


def _is_number(_checker: Any, instance: Any) -> bool:
    """JSON 수. bool이 아니고 유한해야 한다 — NaN·Infinity는 JSON 표준 밖이지만 파이썬 파서는 받아 준다."""
    return (isinstance(instance, int | float) and not isinstance(instance, bool)
            and not (isinstance(instance, float) and not math.isfinite(instance)))


_ContractValidator = validators.extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine("number", _is_number))

_DATE_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?([Zz]|[+-][0-9]{2}:[0-9]{2})\Z")
CONTRACT_FORMATS = FormatChecker()


@CONTRACT_FORMATS.checks("date-time", raises=ValueError)
def _is_date_time(instance: object) -> bool:
    """RFC 3339 날짜-시각이고 서버가 UTC로 바꿀 수 있는 값 (parse_rfc3339와 같은 기준).

    선택 패키지(rfc3339-validator)에 기대지 않는다 — 운영 이미지에는 없어 '2026-13-02T00:00:00Z' 같은 값이 통과했다."""
    if not isinstance(instance, str):
        return True
    if not _DATE_TIME.match(instance):
        raise ValueError("RFC 3339 날짜-시각이 아닙니다")
    parse_rfc3339(instance)
    return True


def contract_validator(schema: dict[str, Any]) -> Draft202012Validator:
    """계약 검증에 쓰는 검증기: 유한한 수만 number, 끝 개행 없는 패턴, 엄격한 date-time."""
    return _ContractValidator(strict_patterns(schema), registry=registry(), format_checker=CONTRACT_FORMATS)


def _too_deep(doc: Any, limit: int = MAX_DEPTH) -> bool:
    """중첩이 limit단계를 넘는가. 재귀하지 않는다(재귀 한도를 넘기는 문서를 안전하게 거르려는 검사다)."""
    stack = [(doc, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict | list):
            if depth > limit:
                return True
            children = node.values() if isinstance(node, dict) else node
            stack.extend((c, depth + 1) for c in children if isinstance(c, dict | list))
    return False


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


TRUNCATED_CODE = "schema.truncated"
ITEM_PROBLEM_LIMIT = 50_000  # 항목별 판정이 필요한 호출(관측값 배치)이 모으는 문제 수 상한. 넘으면 배치 전체를 거부한다


def truncation_notice(limit: int = MAX_PROBLEMS) -> Problem:
    return Problem("", TRUNCATED_CODE, f"문제가 {limit}개를 넘어 앞의 {limit}개만 보여줍니다. 고친 뒤 다시 검사하세요",
                   "WARNING")


def is_truncated(problems: list[Problem]) -> bool:
    return any(p.code == TRUNCATED_CODE for p in problems)


def schema_problems(name: str, doc: Any, *, fragment: str = "", limit: int = MAX_PROBLEMS) -> list[Problem]:
    validator = contract_validator({"$ref": BASE + f"{name}.schema.json{fragment}"})
    # 문제가 수십만 개인 문서에서도 메모리·응답이 커지지 않게 처음 limit+1개까지만 모은다
    errors = sorted(itertools.islice(validator.iter_errors(doc), limit + 1), key=lambda e: list(e.absolute_path))
    out = [Problem(_ptr(e.absolute_path), f"schema.{e.validator}", e.message[:300]) for e in errors[:limit]]
    return out + [truncation_notice(limit)] if len(errors) > limit else out


def _location_problems(doc: dict[str, Any]) -> list[Problem]:
    out = []
    loc = doc.get("location")
    if isinstance(loc, dict):
        out += [Problem(p.pointer, p.code, p.message) for p in check_location(loc)]
    return out


def _observation_problems(doc: dict[str, Any], limit: int = MAX_PROBLEMS) -> list[Problem]:
    out: list[Problem] = []
    items = doc.get("observations")
    if not isinstance(items, list):  # 배열이 아니면 스키마 검사가 말한다
        return out
    for i, ob in enumerate(items):
        if len(out) > limit:  # 더 모아도 담지 않는다
            break
        if not isinstance(ob, dict):
            continue
        base = f"/observations/{i}"
        ts = ob.get("observed_at")
        if isinstance(ts, str):
            try:
                parse_rfc3339(ts)
            except NoOffsetError:
                out.append(Problem(base + "/observed_at", "timezone_required",
                                   "observed_at에는 시간대 오프셋이 필요합니다. 오프셋 없는 원천 시각은 "
                                   "source_timestamp로 보내면 장비 정책의 source_timezone으로 변환합니다"))
            except TimeRangeError:
                out.append(Problem(base + "/observed_at", "invalid_time", "UTC 시각으로 바꿀 수 없는 값입니다"))
            except TimeRuleError:
                pass  # 형식 오류는 스키마(pattern·format) 검사가 구체적으로 말한다
        if ob.get("value") is None and ob.get("quality_flag") not in (None, "MISSING"):
            out.append(Problem(base + "/quality_flag", "missing_flag", "결측값은 quality_flag=MISSING으로 보냅니다"))
        unit = ob.get("unit")
        if isinstance(unit, str) and units().lookup(unit) is None:
            out.append(Problem(base + "/unit", "unknown_unit", "단위 사전에 없는 단위입니다 (SUSPECT로 저장)",
                               "WARNING"))
    return out


SCHEMA_FOR_CONTRACT = {"upload-manifest": "upload-manifest", "observation-batch": "observation-batch",
                       "event": "event", "asset-metadata": "asset-metadata", "agent-heartbeat": "agent-heartbeat"}


def validate_document(contract: str, doc: Any, *, limit: int = MAX_PROBLEMS) -> tuple[list[Problem], bool]:
    """(문제 목록, 스키마 버전 거부 여부). 스키마 버전을 먼저 확인한다 (요구사항 4.3).

    문제 목록은 앞의 limit개와 안내 하나(`schema.truncated`, WARNING)까지다. 항목별로 판정해야 하는 호출(관측값 배치)은
    `limit=ITEM_PROBLEM_LIMIT`로 부르고, 그래도 잘렸으면 `is_truncated`로 알아 배치 전체를 거부한다."""
    if not isinstance(doc, dict):
        return [Problem("", "schema.type", "문서는 JSON 객체여야 합니다")], False
    if _too_deep(doc):
        return [Problem("", "schema.depth", f"문서 중첩은 {MAX_DEPTH}단계까지입니다")], False
    if "schema_version" in doc:  # 없으면 스키마 검증이 필수 필드 누락(400)으로 보고한다
        check = check_schema_version(doc.get("schema_version"), supported_schema_versions())
        if not check.ok:
            return [Problem("/schema_version", "UNSUPPORTED_SCHEMA_VERSION", check.reason or "")], True
    raw = schema_problems(SCHEMA_FOR_CONTRACT[contract], doc, limit=limit)
    location = _location_problems(doc)
    items = _observation_problems(doc, limit) if contract == "observation-batch" else []
    # 같은 값에 대한 스키마 오류와 도메인 오류가 겹치면 도메인 쪽(더 구체적인 사유)을 남긴다.
    # 경고(WARNING)는 아무것도 가리지 않는다. 포인터는 조각 단위로 비교한다 (/observations/10 ≠ /observations/1).
    # - 위치: 좌표 오류는 geometry 전체의 oneOf 불일치(/location)로도 나오므로 /location 아래 스키마 오류를 가린다
    # - 관측 항목: 같은 포인터의 스키마 오류만 가린다 (문서 루트·항목·형제 필드의 오류는 그대로 남는다)
    subtrees = ["/location"] if any(p.severity == "ERROR" for p in location) else []
    exact = {p.pointer for p in items if p.severity == "ERROR"}
    schema = [p for p in raw if p.code != TRUNCATED_CODE and p.pointer not in exact
              and not any(_within(p.pointer, s) for s in subtrees)]
    merged = schema + location + items
    if is_truncated(raw) or len(merged) > limit:  # 앞의 limit개와 안내 하나로 제한한다
        return merged[:limit] + [truncation_notice(limit)], False
    return merged, False


def _segments(pointer: str) -> list[str]:
    return pointer.split("/")[1:] if pointer else []


def _within(pointer: str, scope: str) -> bool:
    """pointer가 scope와 같거나 그 아래인가 (JSON Pointer 조각 단위 비교)."""
    a, b = _segments(pointer), _segments(scope)
    return a[:len(b)] == b
