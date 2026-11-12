import io, sys, zipfile, time
sys.path.insert(0, "/home/user/oceandata/backend")
from xml.dom import minidom
from app.adapters import report_renderers as rr

def mk_template(xml):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("mimetype", "application/hwp+zip", compress_type=zipfile.ZIP_STORED); z.writestr("Contents/section0.xml", xml)
    return b.getvalue()

def doc(s):
    rec = {"record_id": "rec_00000000000000000000000001", "version": 1, "title": s, "test_item": s, "conditions": s, "result": s, "evidence_count": 0}
    return {"title": "T", "status_label": "초안", "version": 1, "generated_at": "g", "data_as_of": "d", "damage_note": None, "variables": {"organization.name": s},
            "sections": [{"kind": "TEXT", "title": "t", "text": "x"},
                         {"kind": "FIELDS", "title": "f", "fields": [{"key": "summary", "label": "L", "value": s, "auto_value": "a", "edited": True}]},
                         {"kind": "RECORDS", "title": "r", "records": [rec]}]}
for name, ch in [("NUL", "\x00"), ("SOH", "\x01"), ("BEL", "\x07"), ("TAB", "\t"), ("LF", "\n"), ("VT", "\x0b"), ("FF", "\x0c"), ("CR", "\r"), ("ESC", "\x1b"), ("US", "\x1f"), ("DEL", "\x7f"), ("U+FFFE", "￾"), ("lone-surrogate", "\ud800"), ("emoji", "\U0001F600")]:
    s = f"a{ch}b"
    res = {}
    for fmt, fn in [("XLSX", lambda: rr.render_xlsx(doc(s))), ("PDF", lambda: rr.render_pdf(doc(s))),
                    ("HWPX", lambda: rr.render_hwpx(mk_template("<sec><p>{{field.summary}}</p></sec>"), doc(s)))]:
        try:
            out = fn()
            if fmt == "HWPX":
                xml = zipfile.ZipFile(io.BytesIO(out)).read("Contents/section0.xml")
                try: minidom.parseString(xml); res[fmt] = "ok"
                except Exception as e: res[fmt] = "MALFORMED-XML"
            else:
                res[fmt] = "ok"
        except BaseException as e:
            res[fmt] = f"EXC:{type(e).__name__}"
    print(f"{name:15s}", res)
