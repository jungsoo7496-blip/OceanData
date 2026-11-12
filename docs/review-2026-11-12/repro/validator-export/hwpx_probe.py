"""HWPX renderer probe: well-formedness / attribute breakout / control chars / zip entry names."""
import io, sys, zipfile
sys.path.insert(0, "/home/user/oceandata/backend")
from xml.dom import minidom
import defusedxml.minidom as dminidom
from app.adapters import report_renderers as rr

def mk_template(xml: str, extra: dict | None = None) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("mimetype", "application/hwp+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("Contents/section0.xml", xml)
        for k, v in (extra or {}).items():
            z.writestr(k, v)
    return b.getvalue()

def doc(**fields):
    base = {"title": "T", "status_label": "초안", "version": 1, "generated_at": "g", "data_as_of": "d", "damage_note": None,
            "variables": {"organization.name": fields.pop("org", "ORG")}, "sections": []}
    base["sections"] = [{"kind": "FIELDS", "title": "f", "fields": [{"key": "summary", "label": "L", "value": fields.get("summary", "v"), "auto_value": "a", "edited": True}]}]
    return base

TEXT_T = '<sec><p a="k">{{field.summary}}</p></sec>'
ATTR_T = '<sec><p a="{{field.summary}}">x</p></sec>'
ATTR_SQ = "<sec><p a='{{field.summary}}'>x</p></sec>"
CDATA_T = '<sec><![CDATA[{{field.summary}}]]></sec>'
COMMENT_T = '<sec><!-- {{field.summary}} --><p>x</p></sec>'
cases = {
    "plain": "ok",
    "lt-gt-amp": "a<b>&c",
    'dq-breakout': 'x" injected="1',
    "sq-breakout": "x' injected='1",
    "cdata-end": "]]><evil/>",
    "comment-end": "--><evil/>",
    "ctrl-0b": "a\x0bb",
    "ctrl-01": "a\x01b",
    "ctrl-1f": "a\x1fb",
    "uffff": "a￿b",
    "bad-entity-like": "&#0;&#x0;&#1;",
}
def wellformed(xml: str):
    try:
        minidom.parseString(xml.encode("utf-8")); return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:60]}"

for tname, tpl in [("text", TEXT_T), ("attr-dq", ATTR_T), ("attr-sq", ATTR_SQ), ("cdata", CDATA_T), ("comment", COMMENT_T)]:
    print(f"--- template position: {tname}")
    for cname, val in cases.items():
        out = rr.render_hwpx(mk_template(tpl), doc(summary=val))
        xml = zipfile.ZipFile(io.BytesIO(out)).read("Contents/section0.xml").decode("utf-8")
        ok, why = wellformed(xml)
        extra = ""
        if ok:
            d = minidom.parseString(xml.encode("utf-8"))
            ps = d.getElementsByTagName("p")
            if ps and ("injected" in dict(ps[0].attributes.items())):
                extra = "  <<< ATTRIBUTE INJECTED"
            if d.getElementsByTagName("evil"):
                extra += "  <<< ELEMENT INJECTED"
        print(f"  {cname:16s} wellformed={ok!s:5s} {why}{extra}")
