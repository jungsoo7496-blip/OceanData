"""Differential fuzz: API-visible problems (validate_document) vs raw jsonschema (schema_problems)."""
import json, random, sys, time, traceback, collections
from fuzzlib import *

seed = int(sys.argv[1]) if len(sys.argv) > 1 else 1
N = int(sys.argv[2]) if len(sys.argv) > 2 else 3000
rng = random.Random(seed)
examples = load_examples()
print("examples:", len(examples), collections.Counter(c for c, _, _ in examples))

crashes = collections.defaultdict(list)
hidden_other = []     # schema problems dropped though no documented rule applies  (should be empty)
flip_valid = []       # raw schema invalid but API shows no ERROR  (should be empty)
bad_pointer = []
t0 = time.time()
stats = collections.Counter()

def resolves(doc, pointer):
    if pointer == "":
        return True
    cur = doc
    for seg in pointer.split("/")[1:]:
        seg = seg.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict):
            if seg not in cur:
                return False
            cur = cur[seg]
        elif isinstance(cur, list):
            if not seg.isdigit() or int(seg) >= len(cur):
                return False
            cur = cur[int(seg)]
        else:
            return False
    return True

for it in range(N):
    contract, name, base = rng.choice(examples)
    doc = mutate(base, rng)
    if not isinstance(doc, dict):
        continue
    try:
        api, rejected = new.validate_document(contract, doc)
    except BaseException as e:
        key = (contract, type(e).__name__, str(e)[:60])
        crashes[key].append(doc if len(json.dumps(doc, default=str)) < 3000 else "(big)")
        stats["crash"] += 1
        continue
    if rejected:
        stats["version_rejected"] += 1
        continue
    raw = new.schema_problems(new.SCHEMA_FOR_CONTRACT[contract], doc)
    api_keys = collections.Counter((p.pointer, p.code, p.message) for p in api)
    raw_keys = [(p.pointer, p.code, p.message) for p in raw]
    stats["docs"] += 1
    # --- pointers must resolve (RFC 6901) to a real node (or its container for required/additionalProperties)
    for p in api:
        if p.code.startswith("schema.") and not resolves(doc, p.pointer):
            bad_pointer.append((contract, p.pointer, p.code, doc))
    # --- every raw schema problem must be present, or be covered by a documented replacement
    loc_err = any(p.severity == "ERROR" for p in new._location_problems(doc))
    item_err_ptrs = set()
    if contract == "observation-batch":
        item_err_ptrs = {p.pointer for p in new._observation_problems(doc) if p.severity == "ERROR"}
    for k in raw_keys:
        if api_keys.get(k, 0) > 0:
            continue
        pointer = k[0]
        covered = (pointer in item_err_ptrs) or (loc_err and (pointer == "/location" or pointer.startswith("/location/")))
        if not covered:
            hidden_other.append((contract, k, doc))
    if raw and not any(p.severity == "ERROR" for p in api):
        flip_valid.append((contract, raw[:2], doc))

print("stats", dict(stats), "elapsed", round(time.time() - t0, 1))
print("CRASH classes:")
for k, v in sorted(crashes.items(), key=lambda kv: -len(kv[1])):
    print("  ", len(v), k)
    sm = [d for d in v if d != "(big)"]
    if sm:
        print("     e.g.", json.dumps(min(sm, key=lambda d: len(json.dumps(d, default=str))), ensure_ascii=False, default=str)[:400])
print("hidden_other:", len(hidden_other), "flip_valid:", len(flip_valid), "bad_pointer:", len(bad_pointer))
for h in hidden_other[:3]:
    print("  HIDDEN", h[0], h[1], json.dumps(h[2], ensure_ascii=False, default=str)[:300])
for h in flip_valid[:3]:
    print("  FLIP", h[0], h[1], json.dumps(h[2], ensure_ascii=False, default=str)[:300])
for h in bad_pointer[:5]:
    print("  BADPTR", h[0], h[1], h[2], json.dumps(h[3], ensure_ascii=False, default=str)[:200])
