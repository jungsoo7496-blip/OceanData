"""계약 글에 적힌 API 경로가 계약에 있는 경로인지 본다 (통합 검토 REPORTS-2).

요약(summary)은 생성 클라이언트(web/src/contract/api.gen.ts)의 주석으로도 나간다. exportReport의 요약은 없는 경로
(/v1/capabilities)를, 같은 작업의 설명은 실제 경로(/v1/system/capabilities)를 가리켜 한 작업이 서로 다른 두 경로를 말했다.
이 시험은 계약(openapi.yaml)과 스키마(schemas/*.json)의 모든 글에서 '/v1/…' 경로를 찾아 계약의 경로 틀({…}은 아무 한
단계)에 맞는지 본다. DB에는 연결하지 않는다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

import yaml

from app.services.contracts import contracts_dir

# 글 속 경로: '/v1/'에서 경로에 쓰는 글자까지. 문장 끝의 '.'과 끝 '/'는 떼고 본다
CITED = re.compile(r"/v1/[A-Za-z0-9_{}\-/.]*")


def _strings(node: Any, where: str) -> Iterator[tuple[str, str]]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(value, f"{where}/{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _strings(value, f"{where}/{i}")
    elif isinstance(node, str):
        yield where, node


def _template(path: str) -> re.Pattern[str]:
    return re.compile("/".join("[^/]+" if part.startswith("{") and part.endswith("}") else re.escape(part)
                               for part in path.split("/")))


def test_every_api_path_cited_in_contract_text_is_a_contract_path() -> None:
    root = contracts_dir()
    api = yaml.safe_load((root / "openapi.yaml").read_text(encoding="utf-8"))
    templates = [_template(p) for p in api["paths"]]
    docs = {"openapi.yaml": api} | {f"schemas/{f.name}": json.loads(f.read_text(encoding="utf-8"))
                                    for f in sorted((root / "schemas").glob("*.json"))}
    cited, unknown = 0, []
    for name, doc in docs.items():
        for where, text in _strings(doc, f"{name}#"):
            for ref in CITED.findall(text):
                cited += 1
                if not any(t.fullmatch(ref.rstrip("./")) for t in templates):
                    unknown.append(f"{where}: {ref}")
    assert cited > 10  # 글에서 경로를 실제로 찾았다
    assert unknown == [], "계약 글이 계약에 없는 경로를 가리킨다"
