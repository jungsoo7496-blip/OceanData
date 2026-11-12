"""PDF render time: which field shape is slow? (single record unless n given)"""
import sys, time
sys.path.insert(0, "/home/user/oceandata/backend")
from app.adapters import report_renderers as rr
shape, n = sys.argv[1], int(sys.argv[2])
sent = "해상 시험 결과 양호하며 특이사항 없음. "
def text(k): return (sent * (k // len(sent) + 1))[:k]
rec = {"record_id": "rec_00000000000000000000000001", "version": 1, "title": text(60), "test_item": text(60), "conditions": text(200), "result": text(400), "evidence_count": 3}
if shape == "long-title": rec["title"] = "t" * 200
if shape == "long-testitem": rec["test_item"] = "i" * 200
if shape == "long-result": rec["result"] = "r" * 4000
if shape == "long-cond": rec["conditions"] = "c" * 2000
if shape == "single-letter-words": rec["result"] = "r " * 2000; rec["conditions"] = "c " * 1000
doc = {"title": "T", "status_label": "초안", "version": 1, "generated_at": "g", "data_as_of": "d", "damage_note": None, "variables": {},
       "sections": [{"kind": "RECORDS", "title": "r", "records": [rec] * n}]}
t = time.perf_counter(); pdf = rr.render_pdf(doc); dt = time.perf_counter() - t
print(f"shape={shape:20s} records={n:3d}  pdf={dt:7.2f}s size={len(pdf)/1e6:.2f}MB", flush=True)
