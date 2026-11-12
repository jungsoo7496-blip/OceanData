import json, sys, re
sys.path.insert(0, "/home/user/oceandata/backend")
from app.services.contracts import validate_document
DEV = "dev_00000000000000000000000303"
OBS = {"metric": "sea_water_temperature", "observed_at": "2026-11-02T00:00:00Z", "value": 18.2, "unit": "degC", "quality_flag": "GOOD"}
base = {"schema_version": "1.0.0", "source_system_id": DEV}
def show(label, contract, doc):
    probs, rej = validate_document(contract, doc)
    print(f"{label:55s} rejected={rej} errors={[(p.pointer,p.code) for p in probs if p.severity=='ERROR']}")
show("obs: metric='...\\n'", "observation-batch", {**base, "observations": [{**OBS, "metric": "sea_water_temperature\n"}]})
show("obs: observed_at='...Z\\n'", "observation-batch", {**base, "observations": [{**OBS, "observed_at": "2026-11-02T00:00:00Z\n"}]})
show("obs: source_system_id + \\n", "observation-batch", {"schema_version": "1.0.0", "source_system_id": DEV + "\n", "observations": [OBS]})
show("obs: session_id + \\n", "observation-batch", {**base, "session_id": "ses_00000000000000000000000401\n", "observations": [OBS]})
show("obs: schema_version '1.0.0\\n'", "observation-batch", {"schema_version": "1.0.0\n", "source_system_id": DEV, "observations": [OBS]})
m = json.load(open("/home/user/oceandata/contracts/examples/upload-manifest.vessel-video.json"))
show("manifest: checksum + \\n", "upload-manifest", {**m, "file": {**m["file"], "checksum_sha256": m["file"]["checksum_sha256"] + "\n"}})
show("manifest: mime 'video/mp4\\n'", "upload-manifest", {**m, "file": {**m["file"], "mime_type": "video/mp4\n"}})
show("manifest: source_system_id + \\n", "upload-manifest", {**m, "source_system_id": m["source_system_id"] + "\n"})
print("python re:", bool(re.search(r"^[0-9a-f]{64}$", "a"*64+"\n")), " ECMA-262 (no m flag) would be: False")
