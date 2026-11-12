import sys, time
sys.path.insert(0, "/home/user/oceandata/backend")
from app.adapters import report_renderers as rr
n = int(sys.argv[1]); mode = sys.argv[2]
sent = "해상 시험 결과 양호하며 특이사항 없음. "
def text(k): return (sent * (k // len(sent) + 1))[:k]
rec = {"record_id": "rec_00000000000000000000000001", "version": 1, "title": "t" * 200, "test_item": "i" * 200, "conditions": text(2000), "result": text(4000), "evidence_count": 3}
if mode == "title-only-long": rec["test_item"] = text(60)
if mode == "both-long-short-text": rec["conditions"] = text(200); rec["result"] = text(400)
if mode == "words-30": rec["title"] = ("w" * 30 + " ") * 6; rec["test_item"] = ("v" * 30 + " ") * 6
doc = {"title": "T", "status_label": "초안", "version": 1, "generated_at": "g", "data_as_of": "d", "damage_note": None, "variables": {},
       "sections": [{"kind": "RECORDS", "title": "r", "records": [rec] * n}]}
t = time.perf_counter(); pdf = rr.render_pdf(doc); dt = time.perf_counter() - t
print(f"mode={mode:22s} records={n:3d}  pdf={dt:7.2f}s size={len(pdf)/1e6:.2f}MB", flush=True)
