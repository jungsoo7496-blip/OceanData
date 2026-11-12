import json, sys
sys.path.insert(0, "/home/user/oceandata/backend")
from app.services.contracts import validate_document
def nest(d): return "[" * d + "1" + "]" * d
lo, hi = 9000, 20000
while lo + 1 < hi:
    mid = (lo + hi) // 2
    try: json.loads(nest(mid)); lo = mid
    except RecursionError: hi = mid
print("max loadable depth ~", lo)
m = json.load(open("/home/user/oceandata/contracts/examples/upload-manifest.vessel-video.json"))
def deepstack(n, f):
    if n == 0: return f()
    return deepstack(n - 1, f)
for d in [lo, lo - 50, lo - 200, lo-1000]:
    deep = json.loads(nest(d))
    for label, doc in [("original_timestamps", {**m, "original_timestamps": {"k": deep}}), ("area_code", {**m, "area_code": deep})]:
        for stack in (0, 300):
            try:
                if stack: deepstack(stack, lambda: validate_document("upload-manifest", doc))
                else: validate_document("upload-manifest", doc)
                print(f"depth {d} {label} stack+{stack}: ok")
            except BaseException as e:
                print(f"depth {d} {label} stack+{stack}: CRASH {type(e).__name__}")
