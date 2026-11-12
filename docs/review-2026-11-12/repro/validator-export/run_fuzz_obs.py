"""Fuzz the observation-batch classification path (replicates api/observations.py post_observations lines 79-93)
and check: items that would reach ingest_items must satisfy the per-item schema; batch-level schema errors must be fatal."""
import json, random, sys, collections, copy
from fuzzlib import *
from jsonschema import Draft202012Validator, FormatChecker
from app.api.observations import _item_reason

seed = int(sys.argv[1]); N = int(sys.argv[2])
rng = random.Random(seed)
obs_examples = [(c, n, d) for c, n, d in load_examples() if c == "observation-batch"]
item_validator = Draft202012Validator({"$ref": new.BASE + "observation-batch.schema.json#/$defs/observation"}, registry=new.registry(), format_checker=FormatChecker())
cnt = collections.Counter(); violations = []; reasons = collections.Counter()
for it in range(N):
    _, name, base = rng.choice(obs_examples)
    doc = mutate(base, rng)
    if not isinstance(doc, dict): continue
    try:
        problems, rejected = new.validate_document("observation-batch", doc)
    except BaseException as e:
        cnt["crash:" + type(e).__name__] += 1; continue
    if rejected: cnt["version_rejected"] += 1; continue
    item_reasons = {}; fatal = []
    for x in problems:
        reason = _item_reason(x)
        if reason is None: fatal.append(x)
        else: item_reasons.setdefault(int(x.pointer.split("/")[2]), reason)
    fatal_err = [x for x in fatal if x.severity == "ERROR"]
    if fatal_err:
        cnt["400"] += 1; continue
    cnt["200"] += 1
    items = doc["observations"]
    kept = [i for i in range(len(items)) if i not in item_reasons]
    for i in kept:
        errs = list(item_validator.iter_errors(items[i]))
        if errs:
            violations.append(("KEPT ITEM VIOLATES ITEM SCHEMA", i, items[i], [e.message[:80] for e in errs[:3]]))
    # batch-level: the doc minus per-item errors must otherwise be schema-valid
    top = {k: v for k, v in doc.items() if k != "observations"}
    raw = new.schema_problems("observation-batch", doc)
    for p in raw:
        parts = p.pointer.split("/")
        if len(parts) >= 3 and parts[1] == "observations":
            continue
        violations.append(("NON-ITEM schema error not fatal", p.pointer, p.code, p.message[:80]))
    for r in item_reasons.values(): reasons[r] += 1
print("counts", dict(cnt)); print("reasons", dict(reasons)); print("violations", len(violations))
seen = set()
for v in violations:
    key = (v[0], str(v[1:3])[:60])
    if key in seen: continue
    seen.add(key); print("  ", json.dumps(v, ensure_ascii=False, default=str)[:420])
    if len(seen) > 12: break
