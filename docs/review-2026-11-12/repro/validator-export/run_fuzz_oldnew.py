"""Old (5cd3c6e^) vs new validate_document on the same mutated docs: where does the new code report LESS than the old?"""
import json, random, sys, collections
from fuzzlib import *
seed, N = int(sys.argv[1]), int(sys.argv[2])
rng = random.Random(seed); ex = load_examples()
less = []; more = collections.Counter(); same = 0; crashes = collections.Counter()
for _ in range(N):
    contract, name, base = rng.choice(ex)
    doc = mutate(base, rng)
    if not isinstance(doc, dict): continue
    try:
        a = old.validate_document(contract, doc)
        b = new.validate_document(contract, doc)
    except BaseException as e:
        crashes[type(e).__name__] += 1; continue
    ka = collections.Counter((p.pointer, p.code, p.severity) for p in a[0]); kb = collections.Counter((p.pointer, p.code, p.severity) for p in b[0])
    if a[1] != b[1]: less.append(("version-rejected differs", contract, doc)); continue
    lost = ka - kb; gained = kb - ka
    if lost: less.append((dict(lost), contract, doc))
    if gained: more[tuple(sorted({(g[1], g[2]) for g in gained}))] += 1
    if not lost and not gained: same += 1
print("same:", same, " new reports MORE (class counts):", dict(more.most_common(8)))
print("new reports LESS than old in", len(less), "docs; crashes", dict(crashes))
shown = set()
for l in less:
    key = str(l[0])[:80]
    if key in shown: continue
    shown.add(key); print("  LESS:", l[0], l[1], json.dumps(l[2], ensure_ascii=False, default=str)[:260])
    if len(shown) >= 6: break
