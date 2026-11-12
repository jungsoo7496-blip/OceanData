import json, sys, time, resource
sys.path.insert(0, "/home/user/oceandata/backend")
from app.services.contracts import validate_document
DEV = "dev_00000000000000000000000303"
n = int(sys.argv[1]); kind = sys.argv[2]
OBS = {"metric": "sea_water_temperature", "observed_at": "2026-11-02T00:00:00Z", "value": 18.2, "unit": "degC", "quality_flag": "GOOD"}
if kind == "scalars":
    items = [1] * n
elif kind == "valid":
    items = [OBS] * n
elif kind == "badtime":
    items = [{**OBS, "observed_at": "2026-11-02T00:00:00"}] * n
elif kind == "multi":
    items = [{"metric": 5, "observed_at": 5, "value": "x", "unit": 5, "quality_flag": 5, "zzz": 1}] * n
doc = {"schema_version": "1.0.0", "source_system_id": DEV, "observations": items}
body = json.dumps(doc)
t = time.perf_counter()
probs, _ = validate_document("observation-batch", doc)
dt = time.perf_counter() - t
resp = json.dumps({"valid": False, "problems": [p.as_dict() for p in probs]})
print(f"{kind:8s} n={n:8d} request={len(body)/1e6:7.2f}MB  time={dt:7.2f}s  problems={len(probs):9d}  response={len(resp)/1e6:8.2f}MB  maxrss={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024:8.0f}MB")
