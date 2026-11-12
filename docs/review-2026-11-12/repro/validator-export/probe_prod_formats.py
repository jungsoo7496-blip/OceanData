"""Simulate a production image (pip install . => jsonschema WITHOUT the [format] extra, no rfc3339_validator)."""
import sys
# block optional format libs that the prod image would not have (dev-only transitive deps in the venv)
for m in ["rfc3339_validator", "rfc3987", "rfc3986_validator", "fqdn", "isoduration", "uri_template", "webcolors", "jsonpointer"]:
    sys.modules[m] = None
sys.path.insert(0, "/home/user/oceandata/backend")
from jsonschema import FormatChecker
print("active formats:", sorted(FormatChecker().checkers))
from app.services.contracts import validate_document
DEV = "dev_00000000000000000000000301"
OBS = {"metric": "sea_water_temperature", "observed_at": "2026-11-02T00:00:00Z", "value": 18.2, "unit": "degC", "quality_flag": "GOOD"}
for label, ts in [("garbage", "garbage"), ("feb30", "2026-02-30T00:00:00Z"), ("hour25", "2026-11-02T25:00:00Z"), ("pattern-ok-but-invalid-month", "2026-13-02T00:00:00Z")]:
    doc = {"schema_version":"1.0.0","source_system_id":DEV,"observations":[{**OBS,"observed_at":ts}]}
    probs,_ = validate_document("observation-batch", doc)
    print(label, ts, [(p.pointer,p.code,p.severity) for p in probs])
# event started_at
import json
ex = json.load(open("/home/user/oceandata/contracts/examples/event.manual-safety.json"))
for ts in ["2026-02-30T00:00:00Z", "2026-13-02T00:00:00Z", "2026-11-02T25:61:61Z"]:
    d = {**ex, "started_at": ts}
    probs,_ = validate_document("event", d)
    print("event started_at", ts, [(p.pointer,p.code,p.severity) for p in probs])
