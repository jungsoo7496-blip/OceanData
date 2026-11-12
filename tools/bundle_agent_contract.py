#!/usr/bin/env python3
"""06번 Go 생성용 투영: 계약 의미를 바꾸지 않고 로컬 JSON Schema 참조를 모은다."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]


def bundle(source: Path) -> dict:
    source = source.resolve()
    directory = source.parent
    documents: dict[Path, dict] = {}
    added: dict[str, dict] = {}
    names: dict[tuple[Path, str], str] = {}

    def document(path: Path) -> dict:
        if path not in documents:
            if not path.is_relative_to(directory):
                raise ValueError("계약 폴더 밖의 참조는 생성에 사용하지 않습니다")
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("계약 참조 대상은 객체여야 합니다")
            documents[path] = value
        return documents[path]

    def pointer(path: Path, fragment: str) -> dict:
        value = document(path)
        if fragment:
            if not fragment.startswith("/"):
                raise ValueError("이름 앵커 참조는 지원하지 않습니다")
            for segment in fragment[1:].split("/"):
                value = value[segment.replace("~1", "/").replace("~0", "~")]
        if not isinstance(value, dict):
            raise ValueError("생성할 스키마는 객체여야 합니다")
        return value

    def reference(raw: str, base: Path) -> str:
        parsed = urlsplit(raw)
        if parsed.scheme or parsed.netloc or parsed.query:
            raise ValueError("네트워크·쿼리 참조는 생성에 사용하지 않습니다")
        target = (base.parent / unquote(parsed.path)).resolve() if parsed.path else base
        if not target.is_relative_to(directory):
            raise ValueError("계약 폴더 밖의 참조는 생성에 사용하지 않습니다")
        fragment = unquote(parsed.fragment)
        if target == source and fragment.startswith("/components/"):
            return "#" + fragment
        node_key = (target, fragment)
        if node_key not in names:
            label = re.sub(r"[^A-Za-z0-9]+", "_", target.stem + "_" + fragment).strip("_")
            digest = hashlib.sha256((target.relative_to(directory).as_posix() + "#" + fragment).encode()).hexdigest()[:8]
            name = f"External_{label}_{digest}"
            names[node_key] = name  # 순환 참조에서도 같은 이름을 사용한다.
            added[name] = {}
            added[name] = walk(pointer(target, fragment), target)
        return "#/components/schemas/" + names[node_key]

    def walk(value, base: Path):
        if isinstance(value, list):
            return [walk(item, base) for item in value]
        if not isinstance(value, dict):
            return value
        if any(key in value for key in ("$id", "$anchor", "$dynamicAnchor", "$dynamicRef", "$recursiveRef")):
            raise ValueError("기준 URI·동적 앵커가 있는 스키마는 자동 투영 전에 확인해야 합니다")
        result = {key: walk(item, base) for key, item in value.items()}
        if "$ref" in result:
            result["$ref"] = reference(result["$ref"], base)
        return result

    result = walk(copy.deepcopy(document(source)), source)
    result.setdefault("components", {}).setdefault("schemas", {}).update(added)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=ROOT / "contracts/openapi.yaml")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = bundle(args.source)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
