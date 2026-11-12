"""서버(Pydantic 요청 모델)가 계약보다 엄격한 길이·개수·범위 제약이 없는지 (04번 요구사항 10.2, ADR-0035).

계약을 보고 만든 클라이언트가 계약에 맞는 값을 보냈는데 서버가 400을 내면 안 된다. 전체 작업 Schemathesis 탐색에서
`license`·`host`·`filename`·이름·제목의 최소 길이 등 10건이 나와 계약을 맞췄다. 이 시험은 DB 없이 FastAPI가 만든 요청 스키마와
contracts/openapi.yaml의 요청 본문 스키마를 속성별로 비교해 같은 종류의 어긋남이 다시 생기면 실패한다.
"""

from __future__ import annotations

import json
from functools import cache
from typing import Any

import yaml
from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from app.api import (
    admin_policies,
    agents,
    assets,
    auth,
    break_glass,
    catalog,
    context,
    datasets,
    deid,
    external,
    lifecycle,
    models_registry,
    monitoring,
    observations,
    onboarding,
    reports,
    sharing,
    system,
    uploads,
    webhooks,
)
from app.services.contracts import contracts_dir

KEYS = ("minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minItems", "maxItems")
Doc = dict[str, Any]


@cache
def _schema_file(name: str) -> Doc:
    return json.loads((contracts_dir() / "schemas" / name).read_text(encoding="utf-8"))


def _deref(doc: Doc, node: Any) -> tuple[Any, Doc]:
    """($ref·allOf를 푼 노드, 그 노드가 든 문서)."""
    for _ in range(30):
        if not (isinstance(node, dict) and "$ref" in node):
            break
        ref = node["$ref"]
        if ref.startswith("#/"):
            target: Any = doc
            for part in ref[2:].split("/"):
                target = target[part]
            node = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
        else:
            name, _, pointer = ref.removeprefix("./schemas/").removeprefix("./").partition("#")
            doc = _schema_file(name)
            target = doc
            for part in [x for x in pointer.split("/") if x]:
                target = target[part]
            node = target
    if isinstance(node, dict) and "allOf" in node:
        merged = {k: v for k, v in node.items() if k != "allOf"}
        props = dict(merged.get("properties") or {})
        for sub in node["allOf"]:
            sub, _ = _deref(doc, sub)
            props.update(sub.get("properties") or {})
            for k, v in sub.items():
                if k != "properties":
                    merged.setdefault(k, v)
        merged["properties"] = props
        node = merged
    return node, doc


def _non_null(doc: Doc, node: Doc, key: str) -> tuple[Doc, Doc]:
    for option in node.get(key) or []:
        option, odoc = _deref(doc, option)
        if option.get("type") != "null":
            return option, odoc
    return node, doc


def _compare(server: Any, sdoc: Doc, contract: Any, cdoc: Doc, path: str, out: list[str]) -> None:
    server, sdoc = _deref(sdoc, server)
    contract, cdoc = _deref(cdoc, contract)
    if not isinstance(server, dict) or not isinstance(contract, dict):
        return
    if "anyOf" in server:  # Optional[...] (null 허용)은 null이 아닌 쪽을 본다
        server, sdoc = _non_null(sdoc, server, "anyOf")
    for key in ("oneOf", "anyOf"):
        if key in contract:
            contract, cdoc = _non_null(cdoc, contract, key)
    for k in KEYS:
        if k in server and k not in contract:
            out.append(f"{path}: 서버 {k}={server[k]}인데 계약에 없음")
    sp, cp = server.get("properties") or {}, contract.get("properties") or {}
    for name in sp:
        if name in cp:
            _compare(sp[name], sdoc, cp[name], cdoc, f"{path}.{name}", out)
    if "items" in server and "items" in contract:
        _compare(server["items"], sdoc, contract["items"], cdoc, f"{path}[]", out)


def test_server_request_models_are_not_stricter_than_contract() -> None:
    app = FastAPI()
    for module in (admin_policies, agents, assets, auth, break_glass, catalog, context, datasets, deid, external,
                   lifecycle, models_registry, monitoring, observations, onboarding, reports, sharing, system, uploads,
                   webhooks):
        app.include_router(module.router)
    server = get_openapi(title="server", version="1", routes=app.routes)
    contract = yaml.safe_load((contracts_dir() / "openapi.yaml").read_text(encoding="utf-8"))
    out: list[str] = []
    compared = 0
    for path, item in server["paths"].items():
        for method, op in item.items():
            body = (op.get("requestBody") or {}).get("content", {}).get("application/json", {}).get("schema")
            cop = contract["paths"].get(path, {}).get(method)
            cbody = ((cop or {}).get("requestBody") or {}).get("content", {}).get("application/json", {}).get("schema")
            if body and cbody:
                compared += 1
                _compare(body, server, cbody, contract, f"{method.upper()} {path}", out)
    assert compared > 50, compared  # 비교 대상이 거의 없으면 경로 대응이 깨진 것
    assert not out, "서버가 계약보다 엄격한 요청 제약:\n" + "\n".join(out)
