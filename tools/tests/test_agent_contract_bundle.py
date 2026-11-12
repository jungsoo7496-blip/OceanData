"""06번 생성용 참조 투영의 의미 보존·경계·재현성을 검사한다."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bundle_agent_contract import bundle

ROOT = Path(__file__).resolve().parents[2]


def validator(doc: dict, name: str):
    registry = Registry().with_resource("urn:test:bundle", Resource.from_contents(doc, default_specification=DRAFT202012))
    return Draft202012Validator({"$ref": f"urn:test:bundle#/components/schemas/{name}"}, registry=registry,
                               format_checker=FormatChecker())


class AgentContractBundleTests(unittest.TestCase):
    def make_source(self, base: Path, schema: dict) -> Path:
        (base / "schemas").mkdir(parents=True)
        (base / "schemas/thing.json").write_text(json.dumps(schema), encoding="utf-8")
        source = base / "openapi.json"
        source.write_text(json.dumps({"openapi": "3.1.0", "components": {"schemas": {
            "Thing": {"$ref": "./schemas/thing.json"}}}}), encoding="utf-8")
        return source

    def test_conditions_nullable_enum_and_required_are_preserved(self):
        schema = {"$defs": {"n": {"type": ["integer", "null"], "enum": [1, None]}}, "type": "object",
                  "properties": {"n": {"$ref": "#/$defs/n"}, "active": {"type": "boolean"}},
                  "additionalProperties": False, "if": {"properties": {"active": {"const": True}},
                                                        "required": ["active"]},
                  "then": {"required": ["n"]}}
        with tempfile.TemporaryDirectory() as directory:
            derived = validator(bundle(self.make_source(Path(directory), schema)), "Thing")
            original = Draft202012Validator(schema)
            for sample in ({}, {"n": None}, {"n": 1}, {"n": 2}, {"active": True},
                           {"active": True, "n": None}, {"unexpected": "value"}):
                with self.subTest(sample=sample):
                    self.assertEqual(derived.is_valid(sample), original.is_valid(sample))

    def test_recursive_local_schema_is_finite(self):
        schema = {"type": "object", "properties": {"next": {"$ref": "#"}}}
        with tempfile.TemporaryDirectory() as directory:
            doc = bundle(self.make_source(Path(directory), schema))
            self.assertTrue(validator(doc, "Thing").is_valid({"next": {"next": {}}}))
            self.assertLess(len(doc["components"]["schemas"]), 5)

    def test_output_does_not_depend_on_checkout_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = bundle(self.make_source(root / "a", {"type": "string"}))
            second = bundle(self.make_source(root / "other", {"type": "string"}))
            self.assertEqual(first, second)

    def test_network_escape_and_dynamic_references_are_rejected(self):
        cases = ({"$ref": "https://example.invalid/schema.json"}, {"$ref": "../../outside.json"},
                 {"$id": "other.json", "type": "string"}, {"$dynamicRef": "#node"})
        for schema in cases:
            with self.subTest(schema=schema), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    bundle(self.make_source(Path(directory), schema))

    def test_repository_contract_examples_keep_their_results(self):
        source = ROOT / "contracts/openapi.yaml"
        original = yaml.safe_load(source.read_text(encoding="utf-8"))
        doc = bundle(source)
        bindings = {}
        def find_bindings(before, after):
            if isinstance(before, dict):
                ref = before.get("$ref", "")
                if ref.startswith("./schemas/") and "#" not in ref:
                    bindings[Path(ref).name.split(".")[0]] = after["$ref"].rsplit("/", 1)[-1]
                for key, value in before.items():
                    find_bindings(value, after[key])
            elif isinstance(before, list):
                for left, right in zip(before, after, strict=True):
                    find_bindings(left, right)

        find_bindings(original, doc)
        tested = 0
        for invalid, folder in ((False, ROOT / "contracts/examples"), (True, ROOT / "contracts/examples/invalid")):
            for example in folder.glob("*.json"):
                key = example.name.split(".")[0]
                if key not in bindings:
                    continue
                with self.subTest(example=example.name):
                    sample = json.loads(example.read_text(encoding="utf-8"))
                    self.assertEqual(validator(doc, bindings[key]).is_valid(sample), not invalid)
                tested += 1
        self.assertGreaterEqual(tested, 30, "실제 계약 예시 대부분을 투영에서도 검증해야 합니다")


if __name__ == "__main__":
    unittest.main()
