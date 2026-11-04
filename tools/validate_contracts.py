#!/usr/bin/env python3
"""계약 검증 (make contracts-check).

검사 항목
1. contracts/schemas/*.schema.json 이 JSON Schema 2020-12로 올바른가
2. contracts/examples/*.json 은 모두 통과하고 contracts/examples/invalid/*.json 은 모두 실패하는가
   (파일 이름의 첫 부분이 스키마 이름이다: upload-manifest.vessel-video.json → upload-manifest.schema.json)
3. domain.md의 코드값과 common.schema.json의 enum이 같은가, api-conventions.md의 오류 코드와 같은가
4. contracts/codes/ 코드표와 스키마·예시가 서로 맞는가 (목적·격리 사유·이벤트 분류·관측 항목·단위)
5. contracts/openapi.yaml 구조 (3.1, operationId, 경로 변수, $ref 대상, 오류 응답 형식, x-freeze)
   openapi-spec-validator가 있으면 명세 검증도 한다.

필요: pyyaml, jsonschema>=4.18 (선택: openapi-spec-validator)
사용: python tools/validate_contracts.py
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

ROOT = pathlib.Path(__file__).resolve().parents[1]
C = ROOT / "contracts"
S = C / "schemas"
EX = C / "examples"
INV = EX / "invalid"
CODES = C / "codes"
DOMAIN = ROOT / ".kiro" / "steering" / "domain.md"
APICONV = ROOT / ".kiro" / "steering" / "api-conventions.md"

errors: list[str] = []
notes: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)


def load_json(p: pathlib.Path):
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 1. 스키마
def _retrieve(uri: str) -> Resource:
    name = uri.split("#")[0].rsplit("/", 1)[-1]
    path = S / name
    if not path.exists():
        raise FileNotFoundError(uri)
    return Resource.from_contents(load_json(path), default_specification=DRAFT202012)


REGISTRY = Registry(retrieve=_retrieve)
schemas: dict[str, dict] = {}
for p in sorted(S.glob("*.schema.json")):
    try:
        doc = load_json(p)
        Draft202012Validator.check_schema(doc)
        schemas[p.name[: -len(".schema.json")]] = doc
    except Exception as e:  # noqa: BLE001
        err(f"[schema] {p.name}: {e}")


def validator_for(name: str) -> Draft202012Validator:
    return Draft202012Validator(schemas[name], registry=REGISTRY, format_checker=FormatChecker())


def schema_of(example: pathlib.Path) -> str | None:
    stem = example.name.split(".")[0]
    return stem if stem in schemas else None


# ---------------------------------------------------------------- 2. 예시
for p in sorted(EX.glob("*.json")):
    name = schema_of(p)
    if not name:
        err(f"[example] {p.name}: 맞는 스키마가 없음")
        continue
    found = sorted(validator_for(name).iter_errors(load_json(p)), key=lambda e: list(e.absolute_path))
    if found:
        e = found[0]
        where = "/" + "/".join(map(str, e.absolute_path))
        err(f"[example] {p.name}: 통과해야 하는데 실패 — {e.message[:200]} @ {where}")
for p in sorted(INV.glob("*.json")):
    name = schema_of(p)
    if not name:
        err(f"[invalid] {p.name}: 맞는 스키마가 없음")
        continue
    if not list(validator_for(name).iter_errors(load_json(p))):
        err(f"[invalid] {p.name}: 실패해야 하는데 통과함")

# ---------------------------------------------------------------- 3. domain.md·api-conventions.md 일치
common = schemas.get("common", {}).get("$defs", {})


def tokens(text: str) -> set[str]:
    return set(re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", text))


domain = DOMAIN.read_text(encoding="utf-8")
expected: dict[str, set[str] | None] = {}
for field in ["collection_mode", "data_role", "privacy_status", "sensitivity_class", "sharing_scope"]:
    m = re.search(rf"^\| {field} \|[^|]*\|([^|]*)\|", domain, re.M)
    expected[field] = tokens(m.group(1)) if m else None
for field in ["ingest_status", "share_status", "grant_status", "report_status"]:
    m = re.search(rf"^- \*\*{field}\*\*[^:\n]*:(.*)$", domain, re.M)
    expected[field] = tokens(m.group(1)) if m else None
m = re.search(r"quarantine_reason\)가 반드시 붙는다:(.*)$", domain, re.M)
expected["quarantine_reason"] = tokens(m.group(1)) if m else None
m = re.search(r"source: ([A-Z |]+),", domain)
expected["timestamp_source"] = tokens(m.group(1)) if m else None
m = re.search(r"location_precision = ([A-Z_ |]+)", domain)
expected["location_precision"] = tokens(m.group(1)) if m else None
for field, rx in [
    ("data_type", r"data_type\(([^)]*)\)"),
    ("quality_status", r"quality_status\(([^)]*)\)"),
    ("review_result", r"review_result\?\(([^)]*)\)"),
    ("backup_class", r"backup_class\(([^)]*)\)"),
    ("local_copy_policy", r"local_copy_policy\(([^)]*)\)"),
    ("variant", r"variant\(([^)]*)\)"),
]:
    m = re.search(rx, domain)
    expected[field] = tokens(m.group(1)) if m else None
m = re.search(r"기관 유형\(org_type\):(.*)$", domain, re.M)
expected["org_type"] = tokens(m.group(1)) if m else None
roles_section = domain.split("## 2.")[1].split("## 3.")[0] if "## 2." in domain else ""
expected["role"] = set(re.findall(r"^\| ([A-Z][A-Z_]{2,}) \|", roles_section, re.M)) or None

for field, want in expected.items():
    if not want:
        err(f"[domain] domain.md에서 {field} 값을 찾지 못함 (표·문장 형식이 바뀌었는지 확인)")
        continue
    have = set(common.get(field, {}).get("enum", []))
    if want != have:
        err(f"[domain] {field}: domain.md {sorted(want)} ≠ common.schema.json {sorted(have)}")

apiconv = APICONV.read_text(encoding="utf-8")
codes_in_doc = dict(re.findall(r"^\| ([A-Z][A-Z_]{2,}) \| (\d{3}) \|", apiconv, re.M))
if set(codes_in_doc) != set(common.get("error_code", {}).get("enum", [])):
    err(f"[api] api-conventions.md 오류 코드 {sorted(codes_in_doc)} ≠ common.schema.json error_code")
for code, status in (("RULE_VIOLATION", "422"), ("VALIDATION_ERROR", "400")):
    if codes_in_doc.get(code) != status:
        err(f"[api] {code}의 HTTP 상태가 problem.schema.json 규칙({status})과 다름")

# ---------------------------------------------------------------- 4. 코드표
def code_set(file: str, key: str) -> set[str]:
    return {row["code"] for row in load_json(CODES / file)[key]}


if code_set("purposes.json", "purposes") != set(common.get("purpose", {}).get("enum", [])):
    err("[codes] purposes.json ≠ common.schema.json purpose")
if code_set("quarantine-reasons.json", "reasons") != set(common.get("quarantine_reason", {}).get("enum", [])):
    err("[codes] quarantine-reasons.json ≠ common.schema.json quarantine_reason")
units = load_json(CODES / "units.json")["units"]
unit_names = {u["code"] for u in units} | {a for u in units for a in u.get("aliases", [])}
canonical_units = {u["code"] for u in units if u.get("canonical")}
for u in units:
    target = u.get("to_canonical", {}).get("unit")
    if target and target not in canonical_units:
        err(f"[codes] units.json {u['code']}의 변환 대상 {target}이 기준 단위가 아님")
metrics = {m_["code"]: m_["unit"] for m_ in load_json(CODES / "metrics.json")["metrics"]}
for code, unit in metrics.items():
    if unit not in canonical_units:
        err(f"[codes] metrics.json {code}의 단위 {unit}이 units.json 기준 단위가 아님")
categories = code_set("event-categories.json", "categories")
for p in sorted(EX.glob("event.*.json")):
    top = load_json(p)["event_code"].split(".")[0]
    if top not in categories:
        err(f"[codes] {p.name}: event_code 상위분류 {top}가 event-categories.json에 없음")
for p in sorted(EX.glob("observation-batch.*.json")):
    for i, o in enumerate(load_json(p)["observations"]):
        if o["metric"] not in metrics:
            err(f"[codes] {p.name} #{i}: metric {o['metric']}가 metrics.json에 없음")
        if "unit" in o and o["unit"] not in unit_names:
            err(f"[codes] {p.name} #{i}: unit {o['unit']}가 units.json에 없음")

# ---------------------------------------------------------------- 5. OpenAPI
api_path = C / "openapi.yaml"
api = yaml.safe_load(api_path.read_text(encoding="utf-8"))
if not str(api.get("openapi", "")).startswith("3.1"):
    err("[openapi] openapi 버전은 3.1.x 여야 함")

_cache: dict[pathlib.Path, object] = {}


def load_doc(path: pathlib.Path):
    path = path.resolve()
    if path not in _cache:
        text = path.read_text(encoding="utf-8")
        _cache[path] = yaml.safe_load(text) if path.suffix in (".yaml", ".yml") else json.loads(text)
    return _cache[path]


def resolve(ref: str, base: pathlib.Path):
    file_part, _, pointer = ref.partition("#")
    target_file = (base.parent / file_part).resolve() if file_part else base.resolve()
    if not target_file.exists():
        raise KeyError(f"파일 없음 {file_part}")
    node = load_doc(target_file)
    for raw in [p for p in pointer.split("/") if p]:
        key = raw.replace("~1", "/").replace("~0", "~")
        node = node[int(key)] if isinstance(node, list) else node[key]
    return node


def walk_refs(node, base: pathlib.Path, where: str) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                try:
                    resolve(v, base)
                except Exception as e:  # noqa: BLE001
                    err(f"[ref] {where}: {v} 를 찾지 못함 ({e})")
            elif k == "externalValue" and isinstance(v, str):
                if not (base.parent / v).resolve().exists():
                    err(f"[ref] {where}: 예시 파일 {v} 없음")
            else:
                walk_refs(v, base, where)
    elif isinstance(node, list):
        for v in node:
            walk_refs(v, base, where)


walk_refs(api, api_path, "openapi.yaml")
for p in sorted(S.glob("*.schema.json")):
    walk_refs(load_json(p), p, p.name)


def deref(node, base=api_path):
    while isinstance(node, dict) and "$ref" in node:
        node = resolve(node["$ref"], base)
    return node


METHODS = {"get", "put", "post", "delete", "patch", "options", "head", "trace"}
NO_PROBLEM_REQUIRED = {"getHealthz", "getReadyz", "getDcatCatalog"}
op_ids: list[str] = []
for path, item in (api.get("paths") or {}).items():
    shared = [deref(x) for x in item.get("parameters", [])]
    for method, op in item.items():
        if method not in METHODS:
            continue
        oid = op.get("operationId")
        if not oid:
            err(f"[openapi] {method.upper()} {path}: operationId 없음")
            continue
        op_ids.append(oid)
        if op.get("x-freeze") not in {"G1", "G4", "G5"}:
            err(f"[openapi] {oid}: x-freeze(G1/G4/G5) 표시 없음")
        params = shared + [deref(x) for x in op.get("parameters", [])]
        declared = {p_["name"] for p_ in params if p_.get("in") == "path" and p_.get("required")}
        for var in re.findall(r"\{([^}]+)\}", path):
            if var not in declared:
                err(f"[openapi] {oid}: 경로 변수 {var} 선언 없음")
        responses = op.get("responses") or {}
        if not responses:
            err(f"[openapi] {oid}: 응답 정의 없음")
        if oid not in NO_PROBLEM_REQUIRED:
            has_problem = False
            for code, resp in responses.items():
                if str(code)[0] in "45":
                    content = deref(resp).get("content", {})
                    if "application/problem+json" in content:
                        has_problem = True
                    else:
                        err(f"[openapi] {oid} {code}: 오류 응답이 application/problem+json이 아님")
            if not has_problem:
                err(f"[openapi] {oid}: problem+json 오류 응답이 하나도 없음")
dups = {x for x in op_ids if op_ids.count(x) > 1}
if dups:
    err(f"[openapi] operationId 중복: {sorted(dups)}")

try:
    from openapi_spec_validator import validate as osv_validate
    from openapi_spec_validator.readers import read_from_filename

    spec_dict, base_uri = read_from_filename(str(api_path))
    osv_validate(spec_dict, base_uri=base_uri)
    notes.append("openapi-spec-validator: 통과")
except ImportError:
    notes.append("openapi-spec-validator 미설치: 구조 검사만 했음 (pip install openapi-spec-validator)")
except Exception as e:  # noqa: BLE001
    err(f"[openapi] openapi-spec-validator 실패: {str(e)[:400]}")

# ---------------------------------------------------------------- 결과
n_ops = len(op_ids)
print(f"스키마 {len(schemas)}개, 예시 {len(list(EX.glob('*.json')))}개(통과 대상), "
      f"{len(list(INV.glob('*.json')))}개(실패 대상), OpenAPI 작업 {n_ops}개")
for n in notes:
    print("  ·", n)
if errors:
    print(f"\n실패 {len(errors)}건:")
    for e in errors:
        print("  ✗", e)
    sys.exit(1)
print("모든 계약 검사 통과")
