import sys, time
sys.path.insert(0, "/home/user/oceandata/backend")
from app.adapters import report_renderers as rr
n = int(sys.argv[1]); mode = sys.argv[2]
rec = {"record_id": "rec_00000000000000000000000001", "version": 1, "title": "t" * 200, "test_item": "i" * 200, "conditions": "c " * 1000, "result": "r " * 2000, "evidence_count": 3}
if mode == "nolong-title-item": rec["title"] = "title text " * 18; rec["test_item"] = "item text " * 20
if mode == "only-long-title-item": rec["conditions"] = "ok"; rec["result"] = "ok"
doc = {"title": "T", "status_label": "초안", "version": 1, "generated_at": "g", "data_as_of": "d", "damage_note": None, "variables": {},
       "sections": [{"kind": "RECORDS", "title": "r", "records": [rec] * n}]}
t = time.perf_counter(); pdf = rr.render_pdf(doc); dt = time.perf_counter() - t
print(f"mode={mode:22s} records={n:3d}  pdf={dt:7.2f}s size={len(pdf)/1e6:.2f}MB", flush=True)
