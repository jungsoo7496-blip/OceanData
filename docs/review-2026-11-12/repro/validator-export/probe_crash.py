"""Crash probes for validate_document (no DB)."""
import json, sys, traceback
sys.path.insert(0, "/home/user/oceandata/backend")
from app.services.contracts import validate_document

DEV = "dev_00000000000000000000000301"
OBS = {"metric": "sea_water_temperature", "observed_at": "2026-11-02T00:00:00Z", "value": 18.2, "unit": "degC", "quality_flag": "GOOD"}

cases = {
 "observations=5": ("observation-batch", {"schema_version":"1.0.0","source_system_id":DEV,"observations":5}),
 "observations=true": ("observation-batch", {"schema_version":"1.0.0","source_system_id":DEV,"observations":True}),
 "observations=1.5": ("observation-batch", {"schema_version":"1.0.0","source_system_id":DEV,"observations":1.5}),
 "observations={}": ("observation-batch", {"schema_version":"1.0.0","source_system_id":DEV,"observations":{"a":1}}),
 "observations='abc'": ("observation-batch", {"schema_version":"1.0.0","source_system_id":DEV,"observations":"abc"}),
 "obs observed_at overflow +23:59": ("observation-batch", {"schema_version":"1.0.0","source_system_id":DEV,"observations":[{**OBS,"observed_at":"0001-01-01T00:00:00+23:59"}]}),
 "obs observed_at overflow -23:59 9999": ("observation-batch", {"schema_version":"1.0.0","source_system_id":DEV,"observations":[{**OBS,"observed_at":"9999-12-31T23:59:59-23:59"}]}),
 "obs observed_at overflow +00:00 0001": ("observation-batch", {"schema_version":"1.0.0","source_system_id":DEV,"observations":[{**OBS,"observed_at":"0001-01-01T00:00:00+01:00"}]}),
 "obs unit non-str": ("observation-batch", {"schema_version":"1.0.0","source_system_id":DEV,"observations":[{**OBS,"unit":5}]}),
 "obs unit list": ("observation-batch", {"schema_version":"1.0.0","source_system_id":DEV,"observations":[{**OBS,"unit":["a"]}]}),
 "obs quality_flag dict": ("observation-batch", {"schema_version":"1.0.0","source_system_id":DEV,"observations":[{**OBS,"value":None,"quality_flag":{"a":1}}]}),
 "loc LineString coords=5": ("upload-manifest", {"schema_version":"1.0.0","location":{"type":"LineString","coordinates":5}}),
 "loc Polygon coords=[1]": ("upload-manifest", {"schema_version":"1.0.0","location":{"type":"Polygon","coordinates":[1]}}),
 "loc Polygon coords=[None]": ("upload-manifest", {"schema_version":"1.0.0","location":{"type":"Polygon","coordinates":[None]}}),
 "loc Polygon coords=7": ("upload-manifest", {"schema_version":"1.0.0","location":{"type":"Polygon","coordinates":7}}),
 "loc Point bigint": ("upload-manifest", {"schema_version":"1.0.0","location":{"type":"Point","coordinates":[10**400, 1]}}),
 "loc Point big float inf": ("upload-manifest", {"schema_version":"1.0.0","location":{"type":"Point","coordinates":[float("inf"), 1]}}),
 "loc Point nan": ("upload-manifest", {"schema_version":"1.0.0","location":{"type":"Point","coordinates":[float("nan"), 1]}}),
 "loc type list": ("upload-manifest", {"schema_version":"1.0.0","location":{"type":["Point"],"coordinates":[1,2]}}),
 "loc type dict": ("upload-manifest", {"schema_version":"1.0.0","location":{"type":{"a":1},"coordinates":[1,2]}}),
 "loc as list": ("upload-manifest", {"schema_version":"1.0.0","location":[1,2]}),
 "loc as str": ("upload-manifest", {"schema_version":"1.0.0","location":"x"}),
 "schema_version int": ("observation-batch", {"schema_version":5}),
 "schema_version list": ("observation-batch", {"schema_version":[1]}),
 "schema_version dict": ("observation-batch", {"schema_version":{"a":1}}),
 "schema_version huge major": ("observation-batch", {"schema_version":"9"*5000+".0.0"}),
 "schema_version None": ("observation-batch", {"schema_version":None}),
 "schema_version ''": ("observation-batch", {"schema_version":""}),
 "schema_version '1.0.0\\n'": ("observation-batch", {"schema_version":"1.0.0\n"}),
 "heartbeat empty": ("agent-heartbeat", {}),
 "event empty": ("event", {}),
 "asset-metadata empty": ("asset-metadata", {}),
}
for name,(contract,doc) in cases.items():
    try:
        probs, rej = validate_document(contract, doc)
        print(f"OK    {name}: rejected={rej} n={len(probs)} first={[ (p.pointer,p.code,p.severity) for p in probs[:3]]}")
    except BaseException as e:
        print(f"CRASH {name}: {type(e).__name__}: {str(e)[:120]}")
