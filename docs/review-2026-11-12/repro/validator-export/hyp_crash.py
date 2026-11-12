"""Hypothesis crash search: arbitrary JSON in the positions the domain checks touch."""
import json, sys, collections
sys.path.insert(0, "/home/user/oceandata/backend")
from hypothesis import given, settings, HealthCheck, strategies as st, seed, Phase
from app.services.contracts import validate_document

jsonval = st.recursive(
    st.none() | st.booleans() | st.integers(min_value=-10**30, max_value=10**30) | st.floats(allow_nan=True, allow_infinity=True) | st.text(max_size=20),
    lambda children: st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=6), children, max_size=4), max_leaves=8)
DEV = "dev_00000000000000000000000301"
OBS = {"metric": "sea_water_temperature", "observed_at": "2026-11-02T00:00:00Z", "value": 18.2, "unit": "degC", "quality_flag": "GOOD"}
crash = collections.OrderedDict()

def record(kind, doc, e):
    key = (kind, type(e).__name__)
    if key not in crash:
        crash[key] = (json.dumps(doc, default=str, ensure_ascii=False)[:300], str(e)[:80])

@settings(max_examples=4000, deadline=None, suppress_health_check=list(HealthCheck), phases=[Phase.generate], database=None)
@seed(20260612)
@given(loc=jsonval, obs=jsonval, item=st.dictionaries(st.sampled_from(["metric", "observed_at", "source_timestamp", "value", "unit", "quality_flag", "depth_m", "channel", "location", "x"]), jsonval, max_size=6))
def run(loc, obs, item):
    for contract, doc in [
        ("upload-manifest", {"schema_version": "1.0.0", "location": loc}),
        ("event", {"schema_version": "1.0.0", "location": loc}),
        ("asset-metadata", {"schema_version": "1.0.0", "location": loc}),
        ("observation-batch", {"schema_version": "1.0.0", "source_system_id": DEV, "observations": obs}),
        ("observation-batch", {"schema_version": "1.0.0", "source_system_id": DEV, "observations": [item]}),
        ("observation-batch", {"schema_version": "1.0.0", "source_system_id": DEV, "observations": [{**OBS, **item}]}),
    ]:
        try:
            validate_document(contract, doc)
        except BaseException as e:
            record(contract, doc, e)

run()
for k, v in crash.items():
    print(k, "\n    ", v[0], "\n    ", v[1])
print("distinct crash classes:", len(crash))
