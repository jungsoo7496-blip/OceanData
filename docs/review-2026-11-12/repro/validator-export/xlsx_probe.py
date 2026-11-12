"""XLSX injection probe: render with hostile strings, then inspect the raw OOXML (no <f>, t=s, quotePrefix)."""
import io, re, sys, zipfile, json, collections
sys.path.insert(0, "/home/user/oceandata/backend")
from app.adapters import report_renderers as rr

def doc_with(s):
    rec = {"record_id": "rec_00000000000000000000000001", "version": 1, "title": s, "test_item": s, "conditions": s, "result": s, "evidence_count": 0}
    return {"title": s, "status_label": "초안", "version": 1, "generated_at": "2026-11-02 09:00 KST", "data_as_of": "2026-11-02 09:00 KST",
            "damage_note": s, "variables": {},
            "sections": [{"kind": "TEXT", "title": s, "text": s},
                         {"kind": "FIELDS", "title": "입력", "fields": [{"key": "k", "label": s, "value": s, "auto_value": s, "edited": True}]},
                         {"kind": "METRICS", "title": "지표", "metrics": [{"metric_code": s, "name": s, "value": 1.5, "unit": s, "status": "OK", "formula": s,
                                                                      "metric_version": 1, "input_count": 1, "missing_count": 0,
                                                                      "sources": {"event_ids": [], "asset_ids": [], "observation_ranges": []}}]},
                         {"kind": "RECORDS", "title": "기록", "records": [rec]}]}

def inspect(xlsx: bytes):
    z = zipfile.ZipFile(io.BytesIO(xlsx))
    out = {"formula_nodes": 0, "t_f": 0, "inlineStr": 0, "t_s": 0, "t_e": 0, "files": z.namelist()}
    for n in z.namelist():
        if n.startswith("xl/worksheets/sheet"):
            x = z.read(n).decode()
            out["formula_nodes"] += len(re.findall(r"<f[ >/]", x))
            out["t_f"] += len(re.findall(r'\bt="f"', x))
            out["inlineStr"] += len(re.findall(r't="inlineStr"', x))
            out["t_s"] += len(re.findall(r'\bt="s"', x))
            out["t_e"] += len(re.findall(r'\bt="e"', x))
    out["quotePrefix_xfs"] = z.read("xl/styles.xml").decode().count('quotePrefix="1"')
    return out, z

LEADS = ["=", "+", "-", "@", "\t", "\r", "\n", " ", " ", "​", "﻿", "＝", "＋", "－", "＠", " ", "\u0085", "\x0c", "−", "‮", "'", "\"", " \t", "​=", "﻿=", " ="]
print("lead -> formula_nodes t_f quotePrefix_xfs (all cells contain  LEAD + 'HYPERLINK(\"http://a.invalid\",\"x\")' )")
for lead in LEADS:
    s = lead + 'HYPERLINK("http://a.invalid","x")'
    try:
        info, _ = inspect(rr.render_xlsx(doc_with(s)))
    except Exception as e:
        print(repr(lead).ljust(12), "EXC", type(e).__name__, str(e)[:60]); continue
    print(repr(lead).ljust(12), info["formula_nodes"], info["t_f"], info["quotePrefix_xfs"], "inlineStr=%d t_s=%d" % (info["inlineStr"], info["t_s"]))
