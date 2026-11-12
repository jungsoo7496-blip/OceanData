"""PDF render time vs. text size, realistic text (Korean sentences) -- measures only render_pdf."""
import sys, time, resource
sys.path.insert(0, "/home/user/oceandata/backend")
from app.adapters import report_renderers as rr
n, rlen, clen = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
sent = "해상 시험 결과 양호하며 특이사항 없음. "
def text(k): return (sent * (k // len(sent) + 1))[:k]
rec = {"record_id": "rec_00000000000000000000000001", "version": 1, "title": text(60), "test_item": text(60), "conditions": text(clen), "result": text(rlen), "evidence_count": 3}
doc = {"title": "T", "status_label": "초안", "version": 1, "generated_at": "g", "data_as_of": "d", "damage_note": None, "variables": {},
       "sections": [{"kind": "RECORDS", "title": "r", "records": [rec] * n}]}
t = time.perf_counter(); pdf = rr.render_pdf(doc); dt = time.perf_counter() - t
print(f"records={n:4d} result={rlen} conditions={clen}  pdf={dt:6.2f}s size={len(pdf)/1e6:.2f}MB maxrss={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024:.0f}MB", flush=True)
