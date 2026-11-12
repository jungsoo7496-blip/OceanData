import json, sys, time, resource
sys.path.insert(0, "/home/user/oceandata/backend")
from app.services.contracts import validate_document
m = json.load(open("/home/user/oceandata/contracts/examples/upload-manifest.vessel-video.json"))
n = int(sys.argv[1])
doc = {**m, "location": {"type": "LineString", "coordinates": "x" * n}}
body = json.dumps(doc)
t = time.perf_counter()
probs, _ = validate_document("upload-manifest", doc)
dt = time.perf_counter() - t
resp = json.dumps({"errors": [p.as_dict() for p in probs]})
print(f"coordinates=<{n} chars> request={len(body)/1e6:.2f}MB time={dt:.2f}s problems={len(probs)} response={len(resp)/1e6:.1f}MB maxrss={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024:.0f}MB")
