import json, sys, time
sys.path.insert(0, "/home/user/oceandata/backend")
sys.setrecursionlimit(1000)
from app.services.contracts import validate_document

def nest(d, leaf=1):
    s = "[" * d + str(leaf) + "]" * d
    return s

# find max depth json.loads accepts
lo = 0
for d in [100, 500, 900, 990, 1000, 2000, 5000, 9000, 20000, 100000]:
    try:
        json.loads(nest(d)); lo = d
    except RecursionError:
        print("json.loads RecursionError at depth", d); break
print("json.loads ok up to at least", lo)

m = json.load(open("/home/user/oceandata/contracts/examples/upload-manifest.vessel-video.json"))
for d in [50, 200, 500, 900, 990, 1500, 3000, 9000]:
    try:
        deep = json.loads(nest(d))
    except RecursionError:
        print("depth", d, ": json.loads fails (-> FastAPI 400)"); continue
    for label, doc in [("original_timestamps value", {**m, "original_timestamps": {"k": deep}}),
                       ("note-ish field wrong type", {**m, "area_code": deep}),
                       ("unknown key", {**m, "zzz": deep})]:
        try:
            t = time.perf_counter(); probs, _ = validate_document("upload-manifest", doc); dt = time.perf_counter() - t
            print(f"depth {d:5d} {label:28s}: ok  n={len(probs)} {dt*1000:.0f}ms")
        except BaseException as e:
            print(f"depth {d:5d} {label:28s}: CRASH {type(e).__name__}: {str(e)[:80]}")
