"""Shared helpers for the contract-validator differential fuzzers."""
from __future__ import annotations
import copy, glob, json, os, random, sys
sys.path.insert(0, "/home/user/oceandata/backend")
sys.path.insert(0, os.path.dirname(__file__))

from app.services import contracts as new
import contracts_old as old  # 5cd3c6e^ version, same package context

EX = "/home/user/oceandata/contracts/examples"
CONTRACT_OF_PREFIX = {"upload-manifest": "upload-manifest", "observation-batch": "observation-batch",
                      "event": "event", "asset-metadata": "asset-metadata", "agent-heartbeat": "agent-heartbeat"}


def load_examples() -> list[tuple[str, str, dict]]:
    out = []
    for path in sorted(glob.glob(EX + "/*.json") + glob.glob(EX + "/invalid/*.json")):
        base = os.path.basename(path)
        for pre, contract in CONTRACT_OF_PREFIX.items():
            if base.startswith(pre + "."):
                out.append((contract, base, json.load(open(path))))
    return out


def ptr(path) -> str:
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in path) if path else ""


def walk(node, path=()):
    """All (path, value) pairs in node."""
    yield path, node
    if isinstance(node, dict):
        for k, v in node.items():
            yield from walk(v, path + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, path + (i,))


def get(node, path):
    for p in path:
        node = node[p]
    return node


def set_(root, path, value):
    if not path:
        return value
    parent = get(root, path[:-1])
    parent[path[-1]] = value
    return root


def delete(root, path):
    parent = get(root, path[:-1])
    if isinstance(parent, list):
        del parent[path[-1]]
    else:
        del parent[path[-1]]


JUNK = [None, True, False, 0, -1, 1, 3.5, -0.0, 10**400, 10**30, float("inf"), float("-inf"), float("nan"), "", " ", "x", "x" * 300,
        "x" * 70000, "2026-11-02T00:00:00", "2026-11-02T00:00:00Z", "2026-11-02T00:00:00Z\n", "2026-13-45T99:99:99Z", "0001-01-01T00:00:00+23:59",
        "9999-12-31T23:59:59-23:59", "dev_00000000000000000000000301", "dev_00000000000000000000000301\n", "GOOD", "MISSING", "degC", "zzz",
        "a/b~c", "~0", "~1", "\u0000", "😀", "한글", "‮", [], {}, [[]], [{}], [1], [1, 2], [0, 0], [200, 100], [100, 200], [1, 2, 3, 4],
        {"type": "Point", "coordinates": [0, 0]}, {"type": "Point", "coordinates": [200, 10]}, {"type": "Point", "coordinates": [10, 200]},
        {"type": "Point", "coordinates": [1, 2], "x": 1}, {"type": "LineString", "coordinates": 5}, {"type": "LineString", "coordinates": [[1, 2]]},
        {"type": "Polygon", "coordinates": [[[0, 1], [1, 1], [1, 2], [0, 1]]]}, {"type": "Polygon", "coordinates": [1]}, {"type": "Polygon", "coordinates": [None]},
        {"type": "Foo", "coordinates": [1, 2]}, {"a": 1}, {"": 1}, {"a/b": 1, "c~d": 2}]

KEYS = ["", "a", "a/b", "~", "~0", "~1", "x~1y", "한글", "observations", "location", "0", "10", "unit", "value", "extra"]


def mutate(doc, rng: random.Random, n_mut: int | None = None):
    doc = copy.deepcopy(doc)
    n = n_mut or rng.choice([1, 1, 1, 2, 2, 3])
    for _ in range(n):
        nodes = list(walk(doc))
        path, val = rng.choice(nodes)
        op = rng.choice(["replace", "replace", "replace", "delete", "addkey", "dup", "grow", "typeswap"])
        try:
            if op == "replace" and path:
                set_(doc, path, copy.deepcopy(rng.choice(JUNK)))
            elif op == "replace":
                doc = copy.deepcopy(rng.choice([d for d in JUNK if isinstance(d, dict)] or [{}]))
            elif op == "delete" and path:
                delete(doc, path)
            elif op == "addkey" and isinstance(val, dict):
                val[rng.choice(KEYS)] = copy.deepcopy(rng.choice(JUNK))
            elif op == "dup" and isinstance(val, list) and val:
                k = rng.choice([2, 5, 10, 11, 12, 25])
                val.extend(copy.deepcopy(val[i % len(val)]) for i in range(k))
            elif op == "grow" and isinstance(val, list) and val:
                # make the list long, then corrupt one element at index >=10 (old prefix bug)
                val.extend(copy.deepcopy(val[0]) for _ in range(12))
                idx = rng.choice([1, 10, 11])
                if isinstance(val[idx], dict) and val[idx]:
                    k = rng.choice(list(val[idx]))
                    val[idx][k] = copy.deepcopy(rng.choice(JUNK))
                else:
                    val[idx] = copy.deepcopy(rng.choice(JUNK))
            elif op == "typeswap" and path:
                set_(doc, path, rng.choice([str(val), [val], {"k": val}, None, 0]))
        except Exception:
            pass
    return doc
