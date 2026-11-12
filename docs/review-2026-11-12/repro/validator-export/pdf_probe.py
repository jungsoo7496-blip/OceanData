"""PDF probe: hostile strings in every user-controlled slot; canary HTTP server detects any fetch by WeasyPrint."""
import html.parser, http.server, json, sys, threading, time, collections
sys.path.insert(0, "/home/user/oceandata/backend")
from app.adapters import report_renderers as rr

hits = []
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        hits.append(self.path); self.send_response(200); self.send_header("Content-Type", "text/css"); self.end_headers(); self.wfile.write(b"body{color:red}")
    def log_message(self, *a): pass
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}"

PAYLOADS = [
    f'<img src="{base}/img-tag">',
    f'<link rel="stylesheet" href="{base}/link-tag">',
    f'</style><style>@import url({base}/import);body{{background:url({base}/bg)}}</style>',
    f"'; background:url({base}/quote-breakout); content:'",
    f'"><img src="{base}/attr-breakout">',
    f'<svg><image href="{base}/svg"/></svg>',
    '<img src="file:///etc/hostname">',
    f'<div style="background:url({base}/style-attr)">x</div>',
    '<script>document.write(1)</script>',
    "&lt;img src=x&gt; &amp;#60;",
    "\\27 ; } @import url(" + base + "/css-escape);",
]
def doc(s):
    rec = {"record_id": "rec_00000000000000000000000001", "version": 1, "title": s, "test_item": s, "conditions": s, "result": s, "evidence_count": 0}
    return {"title": s, "status_label": "초안", "version": 1, "generated_at": "g", "data_as_of": "d", "damage_note": s, "variables": {},
            "sections": [{"kind": "TEXT", "title": s, "text": s},
                         {"kind": "FIELDS", "title": s, "fields": [{"key": "k", "label": s, "value": s, "auto_value": s, "edited": True}]},
                         {"kind": "METRICS", "title": s, "metrics": [{"metric_code": s, "name": s, "value": 1.5, "unit": s, "status": "OK", "formula": s, "metric_version": 1, "input_count": 1, "missing_count": 0, "sources": {"event_ids": [], "asset_ids": [], "observation_ranges": []}}]},
                         {"kind": "RECORDS", "title": s, "records": [rec]}]}

class Tags(html.parser.HTMLParser):
    def __init__(self): super().__init__(); self.tags = collections.Counter(); self.attrs = collections.Counter()
    def handle_starttag(self, tag, attrs):
        self.tags[tag] += 1
        for a, _ in attrs: self.attrs[(tag, a)] += 1
allowed_tags = {"html", "head", "meta", "style", "body", "h1", "h2", "p", "span", "table", "tr", "td", "th", "br"}
for p in PAYLOADS:
    t = Tags(); h = rr.to_html(doc(p)); t.feed(h)
    extra = set(t.tags) - allowed_tags
    bad_attrs = {k: v for k, v in t.attrs.items() if k[1] not in ("lang", "charset", "class")}
    # style element content must be the fixed stylesheet only
    style = h.split("<style>")[1].split("</style>")[0]
    css_has = any(x in style for x in ("@import", "url(", base))
    print(f"{p[:44]!r:48s} injected_tags={sorted(extra)} bad_attrs={bad_attrs} css_changed={css_has}")
    pdf = rr.render_pdf(doc(p))
    assert pdf.startswith(b"%PDF")
time.sleep(0.5)
print("canary hits during rendering of all payloads:", hits)
# positive control: show WeasyPrint would fetch if HTML were injected
from weasyprint import HTML
HTML(string=f'<img src="{base}/POSITIVE-CONTROL">').write_pdf()
print("canary hits after positive control:", hits)
