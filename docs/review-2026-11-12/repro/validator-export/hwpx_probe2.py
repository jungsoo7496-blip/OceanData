import io, sys, zipfile
sys.path.insert(0, "/home/user/oceandata/backend")
from app.adapters import report_renderers as rr
doc = {"title": "T", "status_label": "초안", "version": 1, "generated_at": "g", "data_as_of": "d", "damage_note": None, "variables": {}, "sections": []}
def zipped(entries):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for k, v in entries: z.writestr(k, v)
    return b.getvalue()
cases = {
 "utf-16 xml entry": [("Contents/section0.xml", '<?xml version="1.0" encoding="UTF-16"?><s>{{report.title}}</s>'.encode("utf-16"))],
 "euc-kr xml entry": [("Contents/section0.xml", '<?xml version="1.0" encoding="EUC-KR"?><s>한글 {{report.title}}</s>'.encode("euc-kr"))],
 "backslash traversal name": [("..\\..\\evil.xml", "<a/>"), ("Contents/section0.xml", "<s/>")],
 "windows drive name": [("C:/Windows/evil.xml", "<a/>")],
 "absolute-ish //": [("//etc/evil.xml", "<a/>")],
 "dot-dot in middle": [("a/../b.xml", "<a/>")],
 "nul in name": [("a\x00b.xml", "<a/>")],
}
for name, entries in cases.items():
    data = zipped(entries)
    probs = rr.check_hwpx(data)
    try:
        out = rr.render_hwpx(data, doc); r = "render ok; names=" + str(zipfile.ZipFile(io.BytesIO(out)).namelist())
    except BaseException as e:
        r = f"render EXC {type(e).__name__}"
    print(f"{name:26s} check_hwpx={probs}  {r}")
