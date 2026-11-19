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
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker, ValidationError, validators
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from app.domain.metadata import check_schema_version
from app.domain.normalize import (
    LOCATION_PROBLEM_LIMIT,
    NoOffsetError,
    TimeRangeError,
    TimeRuleError,
    UnitDictionary,
    check_location,
    parse_rfc3339,
    resolve_capture_time,
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


def _jsonschema_evaluated_keys() -> Callable[[Any, Any, Any], list[Any]] | None:
    """표준 unevaluatedProperties가 쓰는 '평가된 속성' 함수. 공개 API가 아니라 없어지면 None(표준 보고로 돌아간다)."""
    try:
        from jsonschema import _utils
    except ImportError:  # pragma: no cover
        return None
    return getattr(_utils, "find_evaluated_property_keys_by_schema", None)


_STANDARD_UNEVALUATED = Draft202012Validator.VALIDATORS["unevaluatedProperties"]
_evaluated_keys = _jsonschema_evaluated_keys()


def _unevaluated_properties(validator: Any, unevaluated: Any, instance: Any,
                            schema: dict[str, Any]) -> Iterator[ValidationError]:
    """unevaluatedProperties — 판정은 표준 그대로, 보고만 고친다 (ADR-0067).

    표준은 allOf 항목 하나가 실패하면 그 항목이 평가한 속성을 버린다(실패한 하위 스키마의 주석은 남지 않는다).
    그래서 이벤트 문서의 시각 하나가 틀려도 루트에 '알려진 속성이 모두 평가되지 않음' 오류가 붙었다.
    실패한 항목은 자기 오류로 이미 보고되므로, 그 항목이 평가하는 속성은 평가된 것으로 보고 나머지(계약에 없는
    속성)만 보고한다. allOf가 실패하면 이 스키마는 어차피 무효라 어떤 문서의 유효성도 바뀌지 않는다.

    통과한 항목이 평가하는 속성은 표준도 같은 함수로 이미 센다. 그래서 항목마다 통과 여부를 다시 검사하지 않고
    모든 항목의 속성을 밝힌 항목을 더해 표준을 한 번만 돌린다 (ADR-0086). 항목의 속성은 문서를 그 항목에 비춰
    보지 않고 정한다(하위 allOf·if의 조건만 본다)."""
    members = schema.get("allOf")
    if not isinstance(members, list) or _evaluated_keys is None or not validator.is_type(instance, "object"):
        yield from _STANDARD_UNEVALUATED(validator, unevaluated, instance, schema)
        return
    declared = sorted({str(k) for m in members for k in _evaluated_keys(validator, instance, m)})
    stub = {"properties": dict.fromkeys(declared, True)}  # 항목들이 평가하는 속성만 밝히는 항상 통과하는 항목
    yield from _STANDARD_UNEVALUATED(validator, unevaluated, instance, {**schema, "allOf": [*members, stub]})


def _one_of(validator: Any, one_of: list[Any], instance: Any, schema: dict[str, Any]) -> Iterator[ValidationError]:
    """oneOf — 판정·오류 자리·메시지는 표준 그대로, 맞지 않은 가지의 오류(context)만 모으지 않는다 (ADR-0086).

    표준은 맞는 가지가 없으면 가지마다의 오류를 모두 모아 오류 하나에 달아 둔다. geometry(Point·LineString·Polygon)에서
    좌표 하나가 틀린 큰 항적은 Polygon 가지가 좌표마다 오류를 만들어 문서 1MB에 1GB 넘게 썼다. 문제 목록은 오류의
    자리·키워드·메시지만 쓰므로, 가지가 맞는지는 첫 오류까지만 본다."""
    subschemas = enumerate(one_of)
    for index, subschema in subschemas:
        if next(validator.descend(instance, subschema, schema_path=index), None) is None:
            first_valid = subschema
            break
    else:
        yield ValidationError(f"{instance!r} is not valid under any of the given schemas")
        return
    more_valid = [each for _, each in subschemas if validator.evolve(schema=each).is_valid(instance)]
    if more_valid:
        more_valid.append(first_valid)
        reprs = ", ".join(repr(each) for each in more_valid)
        yield ValidationError(f"{instance!r} is valid under each of {reprs}")


_ContractValidator = validators.extend(
    Draft202012Validator,
    validators={"unevaluatedProperties": _unevaluated_properties, "oneOf": _one_of},
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


# 형상마다 계약에 맞는 좌표 (위치 오류가 가리키는 자리만 이 값으로 바꿔 나머지 결함을 본다)
_SAMPLE_COORDINATES: dict[str, Any] = {"Point": [1, 1], "LineString": [[1, 1], [1, 1]],
                                       "Polygon": [[[1, 1], [1, 1], [1, 1], [1, 1]]]}


def _repaired_location(loc: dict[str, Any], problems: list[Problem]) -> dict[str, Any] | None:
    """위치 오류가 가리키는 좌표 자리만 계약에 맞는 값으로 바꾼 사본. 좌표 하나의 셋째 값부터는 원래 값을 둔다.

    바꾸는 자리까지의 배열만 새로 만들고 나머지 좌표는 원래 객체를 함께 쓴다(원래 문서는 바꾸지 않는다, ADR-0086).
    형상 종류가 틀렸거나(무엇에 비춰 볼지 모른다) 자리를 따라갈 수 없으면 None."""
    kind = loc.get("type")
    if not isinstance(kind, str) or kind not in _SAMPLE_COORDINATES:
        return None
    out = dict(loc)
    fresh: dict[int, list[Any]] = {}  # 이 사본에서 새로 만든 배열 (id → 객체, 살아 있는 동안 id가 겹치지 않는다)
    for p in problems:
        segs = _segments(p.pointer)
        if segs[:2] != ["location", "coordinates"] or not all(s.isdigit() for s in segs[2:]):
            return None
        parent: Any = out
        key: Any = "coordinates"
        sample: Any = _SAMPLE_COORDINATES[kind]
        for s in segs[2:]:
            child = parent.get(key) if isinstance(parent, dict) else parent[key]
            if not isinstance(child, list) or int(s) >= len(child) or not isinstance(sample[0], list):
                return None
            if id(child) not in fresh:  # 원래 문서의 배열은 바꾸지 않고 얕게 복사해 바꾼다
                child = list(child)
                fresh[id(child)] = child
                parent[key] = child
            parent, key, sample = child, int(s), sample[0]
        current = parent.get(key) if isinstance(parent, dict) else parent[key]
        if all(isinstance(x, int) for x in sample):  # 좌표 하나
            parent[key] = [*sample, *(current[2:] if isinstance(current, list) else [])]
        else:  # 좌표 배열 자리에 배열이 아닌 값
            parent[key] = copy.deepcopy(sample)
    return out


def _location_explains_schema(contract: str, doc: dict[str, Any], location: list[Problem],
                              schema: list[Problem]) -> bool:
    """위치 오류가 /location 아래 스키마 오류를 모두 설명하는가 (그럴 때만 스키마 오류를 가린다).

    좌표 오류는 geometry 전체의 oneOf 불일치(/location)로도 나온다. 위치 오류가 가리키는 자리만 고친 사본이
    계약을 통과하면 그 불일치는 위치 오류 때문이다. 통과하지 못하면 다른 결함(계약에 없는 속성, 모자란 좌표 수,
    셋째 값 등)이 있으므로 스키마 오류도 남긴다. 계약의 위치 규칙은 다른 필드에 기대지 않으므로 그 계약의 location
    속성 스키마로만 검사한다(문서 전체 스키마로 검사하면 이벤트의 allOf·unevaluatedProperties를 다시 돈다, ADR-0086)."""
    errors = [p for p in location if p.severity == "ERROR"]
    if not errors or not any(_within(p.pointer, "/location") for p in schema):
        return False  # 가릴 스키마 오류가 없다
    if contract not in _LOCATION_SCHEMA:
        return False  # 문서에 location 속성이 없는 계약 (/location 아래 스키마 오류가 생기지 않는다)
    if len(errors) >= LOCATION_PROBLEM_LIMIT and \
            len(check_location(doc["location"], limit=LOCATION_PROBLEM_LIMIT + 1)) > LOCATION_PROBLEM_LIMIT:
        return False  # 좌표 오류가 수집 상한보다 많다: 모으지 못한 자리를 고칠 수 없으니 스키마 오류도 남긴다
    repaired = _repaired_location(doc["location"], errors)
    if repaired is None:
        return True
    return _location_validator(contract).is_valid(repaired)


# 계약마다 문서 location 속성의 스키마 (위치 재검사가 쓴다). location 속성이 있는 계약은 모두 여기 있어야 한다
_LOCATION_SCHEMA = {"event": "event.schema.json#/$defs/event_body/properties/location",
                    "upload-manifest": "upload-manifest.schema.json#/properties/location",
                    "asset-metadata": "asset-metadata.schema.json#/properties/location"}


@lru_cache(maxsize=4)
def _location_validator(contract: str) -> Draft202012Validator:
    return contract_validator({"$ref": BASE + _LOCATION_SCHEMA[contract]})


@lru_cache(maxsize=1)
def _observed_at_shape() -> Draft202012Validator:
    """관측 항목 observed_at의 계약 모양(type·pattern). format은 보지 않는다."""
    ref = BASE + "observation-batch.schema.json#/$defs/observation/properties/observed_at"
    return _ContractValidator(strict_patterns({"$ref": ref}), registry=registry())


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
            # 시각 힌트는 그것만 틀렸을 때만 붙인다(같은 자리의 스키마 오류를 대신하므로). 그 밖의 형식 오류는
            # 스키마(pattern·format) 검사가 구체적으로 말한다 ('2026-11-02'는 오프셋보다 시각이 없는 것이 문제다)
            try:
                parse_rfc3339(ts)
            except NoOffsetError:
                if _observed_at_shape().is_valid(ts + "Z"):  # 오프셋만 붙이면 계약에 맞는다
                    out.append(Problem(base + "/observed_at", "timezone_required",
                                       "observed_at에는 시간대 오프셋이 필요합니다. 오프셋 없는 원천 시각은 "
                                       "source_timestamp로 보내면 장비 정책의 source_timezone으로 변환합니다"))
            except TimeRangeError:
                if _observed_at_shape().is_valid(ts):  # 모양은 맞고 UTC로 바꾸면 범위를 넘는다
                    out.append(Problem(base + "/observed_at", "invalid_time", "UTC 시각으로 바꿀 수 없는 값입니다"))
            except TimeRuleError:
                pass
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
    # - 위치: 좌표 오류는 geometry 전체의 oneOf 불일치(/location)로도 나온다. 위치 오류가 그 불일치를 모두
    #   설명할 때만 /location 아래 스키마 오류를 가린다 (다른 결함이 있으면 함께 남긴다, ADR-0067)
    # - 관측 항목: 같은 포인터의 스키마 오류만 가린다 (문서 루트·항목·형제 필드의 오류는 그대로 남는다)
    subtrees = ["/location"] if _location_explains_schema(contract, doc, location, raw) else []
    exact = {p.pointer for p in items if p.severity == "ERROR"}
    schema = [p for p in raw if p.code != TRUNCATED_CODE and p.pointer not in exact
              and not any(_within(p.pointer, s) for s in subtrees)]
    merged = schema + location + items
    if is_truncated(raw) or len(merged) > limit:  # 앞의 limit개와 안내 하나로 제한한다
        return merged[:limit] + [truncation_notice(limit)], False
    return merged, False


# ---- 운영 경로가 validate_document 뒤에 따로 보는 문서 규칙 (사전 검증도 같은 함수로 본다, 요구사항 13.1·13.2)
# 운영 경로는 상태 코드와 검사 순서를 지키려고 각자 자리에서 본다: 이벤트 코드표는 422(회차·장비 확인 뒤), 이벤트 시각
# 순서는 400(그 뒤), 관측 항목 코드는 정규화의 항목별 UNKNOWN_METRIC, 매니페스트 시각 순서는 업로드의 400.
# 회차·장비 범위, 사람 확인, 미래 시각, 중복처럼 저장된 상태나 받은 시각에 기대는 규칙은
# 사전 검증이 보지 않는다 (ADR-0081).

@lru_cache(maxsize=4)
def _shape(definition: str) -> Draft202012Validator:
    """common.schema.json의 정의 하나(type·pattern)만 보는 검증기. 모양이 틀린 값은 스키마 오류가 이미 말한다."""
    return _ContractValidator(strict_patterns({"$ref": BASE + "common.schema.json#/$defs/" + definition}),
                              registry=registry())


def event_code_problem(code: str, subcode_registered: Callable[[str], bool]) -> Problem | None:
    """이벤트 코드표 규칙 (03번 요구사항 8.3, 07번 7.5). 공통 상위분류에 있어야 하고, 하위코드는 호출 기관이 등록한
    것이어야 한다. POST /v1/events(422 RULE_VIOLATION)와 사전 검증이 같이 쓴다."""
    if code.split(".")[0] not in {x["code"] for x in code_table("event-categories")["items"]}:
        return Problem("/event_code", "unknown_event_category", "공통 상위분류에 없는 이벤트 코드입니다")
    if "." in code and not subcode_registered(code):
        return Problem("/event_code", "unregistered_event_subcode", "기관 하위코드가 등록되어 있지 않습니다")
    return None


def _instant(value: Any) -> datetime | None:
    """시각 순서를 비교할 UTC 값. 읽히지 않으면 None (형식 오류는 스키마 검사가 말한다)."""
    if not isinstance(value, str):
        return None
    try:
        return parse_rfc3339(value)
    except TimeRuleError:
        return None


def time_order_problems(contract: str, doc: dict[str, Any]) -> list[Problem]:
    """시각 순서 규칙. 두 시각이 모두 읽힐 때만 UTC 값으로 비교한다(문자열로 비교하지 않는다).

    - 이벤트: ended_at < started_at이면 /ended_at before_start. POST /v1/events가 같은 함수로 400을 낸다.
    - 매니페스트: 운영 업로드와 같은 resolve_capture_time으로 본다. 근거 출처일 때만 capture_end_at < capture_start_at을
      /capture_start_at time_rule로 거부하고, 출처가 UNKNOWN이거나 없으면 시각을 버리므로 오류가 아니다."""
    if contract == "event":
        start, end = _instant(doc.get("started_at")), _instant(doc.get("ended_at"))
        if start is not None and end is not None and end < start:
            return [Problem("/ended_at", "before_start", "종료가 시작보다 빠릅니다")]
    elif contract == "upload-manifest":
        start_at, end_at, quality = doc.get("capture_start_at"), doc.get("capture_end_at"), doc.get("timestamp_quality")
        if _instant(start_at) is not None and _instant(end_at) is not None and isinstance(quality, dict):
            try:
                resolve_capture_time(start_at, end_at, quality)
            except TimeRuleError as exc:
                return [Problem("/capture_start_at", "time_rule", str(exc))]
    return []


def rule_problems(contract: str, doc: dict[str, Any], *, subcode_registered: Callable[[str], bool],
                  limit: int = MAX_PROBLEMS) -> list[Problem]:
    """코드표(이벤트 상위분류·기관 하위코드, 관측 항목)와 시각 순서 규칙의 문제. 코드는 모양이 계약에 맞을 때만 본다."""
    out: list[Problem] = []
    if contract == "event":
        code = doc.get("event_code")
        if isinstance(code, str) and _shape("code_ref").is_valid(code):
            problem = event_code_problem(code, subcode_registered)
            out += [problem] if problem is not None else []
    elif contract == "observation-batch" and isinstance(doc.get("observations"), list):
        metrics = {m["code"] for m in code_table("metrics")["items"]}  # 정규화(metrics_table)와 같은 코드표
        for i, ob in enumerate(doc["observations"]):
            if len(out) > limit:  # 더 모아도 담지 않는다
                break
            metric = ob.get("metric") if isinstance(ob, dict) else None
            if isinstance(metric, str) and metric not in metrics and _shape("metric_code").is_valid(metric):
                out.append(Problem(f"/observations/{i}/metric", "unknown_metric",
                                   "관측 항목 코드표(metrics)에 없는 항목입니다 (항목을 UNKNOWN_METRIC으로 거부)"))
    return out + time_order_problems(contract, doc)


def prevalidate(contract: str, doc: Any, *, subcode_registered: Callable[[str], bool],
                limit: int = MAX_PROBLEMS) -> tuple[bool, list[Problem]]:
    """사전 검증(POST /v1/contracts/validate)의 (유효 여부, 문제 목록).

    운영 경로와 같은 validate_document에, 운영 경로가 그 뒤에 보는 문서 규칙(rule_problems)을 더한다. 규칙 문제는
    스키마·위치·항목 문제 뒤에 붙고 목록은 앞의 limit개와 안내 하나까지다. 판정은 목록에서 잘린 문제까지 본다.
    목록에 ERROR가 없는데 잘렸으면(관측값 배치에서 경고가 앞을 채운 경우) 운영 경로가 항목별 판정에 쓰는
    목록(ITEM_PROBLEM_LIMIT까지)을 다시 모아 판정한다. 그 목록도 잘리면 운영 경로가 배치 전체를 거부하므로
    무효다 (ADR-0086).
    스키마 버전 거부나 객체가 아닌·너무 깊은 문서처럼 validate_document가 바로 돌려준 경우에는
    운영 경로처럼 거기서 멈춘다."""
    problems, rejected = validate_document(contract, doc, limit=limit)
    stopped = rejected or not isinstance(doc, dict) or any(p.code == "schema.depth" for p in problems)
    extra = [] if stopped else rule_problems(contract, doc, subcode_registered=subcode_registered, limit=limit)
    valid = not any(p.severity == "ERROR" for p in (*problems, *extra))
    if valid and is_truncated(problems):
        # 보이는 문제가 모두 경고면, 목록 맨 앞에 오는 스키마 오류는 앞쪽 항목의 오류에 가려진 몇 개뿐이다.
        # 그래서 다시 모으는 일은 스키마 검사 한 번과 항목 문제 목록이다 (운영 경로가 같은 문서에 하는 일과 같다)
        full, _ = validate_document(contract, doc, limit=max(limit, ITEM_PROBLEM_LIMIT))
        valid = not is_truncated(full) and not any(p.severity == "ERROR" for p in full)
    if extra:
        merged = [p for p in problems if p.code != TRUNCATED_CODE] + extra
        truncated = is_truncated(problems) or len(merged) > limit
        problems = merged[:limit] + [truncation_notice(limit)] if truncated else merged
    return valid, problems


def _segments(pointer: str) -> list[str]:
    return pointer.split("/")[1:] if pointer else []


def _within(pointer: str, scope: str) -> bool:
    """pointer가 scope와 같거나 그 아래인가 (JSON Pointer 조각 단위 비교)."""
    a, b = _segments(pointer), _segments(scope)
    return a[:len(b)] == b
