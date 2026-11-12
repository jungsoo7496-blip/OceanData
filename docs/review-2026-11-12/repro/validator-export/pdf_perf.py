import sys, time, resource
sys.path.insert(0, "/home/user/oceandata/backend")
from app.adapters import report_renderers as rr
n = int(sys.argv[1])
rec = {"record_id": "rec_00000000000000000000000001", "version": 1, "title": "t" * 200, "test_item": "i" * 200, "conditions": "c " * 1000, "result": "r " * 2000, "evidence_count": 3}
doc = {"title": "T", "status_label": "초안", "version": 1, "generated_at": "g", "data_as_of": "d", "damage_note": None, "variables": {},
       "sections": [{"kind": "RECORDS", "title": "r", "records": [rec] * n}]}
t = time.perf_counter(); pdf = rr.render_pdf(doc); dt = time.perf_counter() - t
t = time.perf_counter(); x = rr.render_xlsx(doc); dx = time.perf_counter() - t
print(f"records={n:4d} pdf={dt:6.2f}s size={len(pdf)/1e6:.2f}MB  xlsx={dx:5.2f}s  maxrss={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024:.0f}MB")
