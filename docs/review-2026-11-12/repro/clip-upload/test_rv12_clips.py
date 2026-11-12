"""RV12 scratch: clip export attack reproductions (throwaway)."""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app.db.models import Asset, AssetLineage, OrgQuota
from app.domain import permissions
from app.domain.principal import Principal
from app.seed import DEV_A_COLLECTOR, ORG_A, ORG_B
from tests.acceptance.test_t09_deid_review import video
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_clips import ADMIN_A, ADMIN_B, MEMBER_A, OP, VIEWER, _clip_jobs, _job, _set_room, _source
from tests.integration.test_pipeline import drain

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg")]
CLIPS = "/v1/assets/{asset_id}/clips"


def obs(*a):
    print("OBS", *a, flush=True)


def audit_rows(ctx):
    with ctx.c.engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(
            "SELECT action, result, actor_kind, organization_id, target_type FROM audit_log ORDER BY seq"))]


def test_permission_matrix(ictx, tmp_path):
    ctx = ictx
    src = _source(ctx, video(tmp_path, "testsrc2=size=320x240:rate=15", "a.mp4"))
    path = CLIPS.format(asset_id=src)
    body = {"start_s": 0, "end_s": 1}
    for name, uid in (("OP", 106), ("OP2", 107), ("VIEWER", 105), ("REVIEWER", 108), ("CONSUMER", 109),
                      ("ADMIN_B", 103), ("MEMBER_B", 104)):
        rp = ctx.client.post(path, json=body, headers=ctx.as_user(uid))
        rg = ctx.client.get(path, headers=ctx.as_user(uid))
        obs("matrix", name, "POST", rp.status_code, "GET", rg.status_code)
    # no auth / bad token
    obs("matrix anon", ctx.client.post(path, json=body).status_code)
    obs("jobs created by denied callers:", _clip_jobs(ctx))
    acts = [(a[0], a[1], a[3]) for a in audit_rows(ctx)]
    from collections import Counter
    obs("audit actions", dict(Counter((a[0], a[1]) for a in acts)))
    # machine principals
    api = Principal(kind="API_KEY", organization_id=ORG_A, org_type="PROVIDER", roles=frozenset(),
                    scopes=frozenset(["asset:read", "upload:write", "status:read", "asset:clip"]))
    dev = Principal(kind="DEVICE", organization_id=ORG_A, org_type="PROVIDER", roles=frozenset({"DEVICE"}),
                    device_id=DEV_A_COLLECTOR, scopes=frozenset(["upload:write", "asset:read", "asset:clip"]))
    obs("machine allowed asset:clip?", permissions.allowed(api, "asset:clip"), permissions.allowed(dev, "asset:clip"))
    # derived variants / wrong type are not clippable
    with ctx.c.sessionmaker.begin() as db:
        a = db.get(Asset, src)
        a.variant = "PROXY"
    obs("clip of PROXY ->", ctx.client.post(path, json=body, headers=ctx.as_user(MEMBER_A)).status_code)
    with ctx.c.sessionmaker.begin() as db:
        a = db.get(Asset, src)
        a.variant = "DEID"
    obs("clip of DEID ->", ctx.client.post(path, json=body, headers=ctx.as_user(MEMBER_A)).status_code)
    with ctx.c.sessionmaker.begin() as db:
        a = db.get(Asset, src)
        a.variant, a.ingest_status = "ORIGINAL", "QUARANTINED"
    obs("clip of QUARANTINED ->", ctx.client.post(path, json=body, headers=ctx.as_user(MEMBER_A)).status_code)
    with ctx.c.sessionmaker.begin() as db:
        a = db.get(Asset, src)
        a.ingest_status, a.legal_hold = "READY_PRIVATE", True
    r = ctx.client.post(path, json=body, headers=ctx.as_user(MEMBER_A))
    obs("clip of LEGAL_HOLD asset ->", r.status_code)
    drain(ctx)
    with ctx.c.sessionmaker() as db:
        clip = db.execute(select(Asset).where(Asset.variant == "CLIP")).scalar_one()
        obs("clip legal_hold inherited?", clip.legal_hold, "retention_policy", clip.retention_policy_id,
            "data_role", clip.data_role, "sens", clip.sensitivity_class, "privacy", clip.privacy_status,
            "scope", clip.sharing_scope, "share_status", clip.share_status, "mode", clip.collection_mode,
            "quality", clip.quality_status, clip.quality_reasons)
        obs("clip provenance keys", sorted(clip.provenance), "has orig filename?", "original_filename" in clip.provenance)
        parent = db.get(Asset, src)
        obs("parent provenance keys", sorted(parent.provenance))
    # delete the clip while parent is on hold -> allowed?
    with ctx.c.sessionmaker() as db:
        cid = db.execute(select(Asset.asset_id).where(Asset.variant == "CLIP")).scalar_one()
    d = ctx.client.post(f"/v1/assets/{cid}/deletion", json={"reason": "legal hold child delete"},
                        headers=ctx.as_user(ADMIN_A, mfa=True))
    obs("delete CLIP of legal-hold parent ->", d.status_code)
    # operator sees clip metadata but cannot download
    obs("operator GET clip meta", ctx.client.get(f"/v1/assets/{cid}", headers=ctx.as_user(OP)).status_code)


def test_range_and_reason_fuzz(ictx, tmp_path):
    ctx = ictx
    src = _source(ctx, video(tmp_path, "testsrc2=size=320x240:rate=15", "a.mp4"))
    path = CLIPS.format(asset_id=src)
    h = ctx.as_user(MEMBER_A)
    cases = {
        "NaN literal": '{"start_s": NaN, "end_s": 1}',
        "Infinity end": '{"start_s": 0, "end_s": Infinity}',
        "neg zero start": '{"start_s": -0.0, "end_s": 1}',
        "1e308": '{"start_s": 0, "end_s": 1e308}',
        "huge int": '{"start_s": 0, "end_s": ' + "9" * 400 + "}",
        "bool": '{"start_s": false, "end_s": true}',
        "string nums": '{"start_s": "0", "end_s": "1"}',
        "tiny range 0.0006": '{"start_s": 1.0, "end_s": 1.0006}',
        "sub-ms equal": '{"start_s": 1.0, "end_s": 1.0004}',
        "end=4.0004 (dur 4)": '{"start_s": 3.0, "end_s": 4.0004}',
        "end=4.0006": '{"start_s": 3.0, "end_s": 4.0006}',
        "null end": '{"start_s": 0, "end_s": null}',
        "reason trailing \\n": '{"start_s": 0, "end_s": 1.1, "reason": "ab\\n"}',
        "reason U+2028": '{"start_s": 0, "end_s": 1.2, "reason": "ab\\u2028cd"}',
        "reason U+0085": '{"start_s": 0, "end_s": 1.3, "reason": "ab\\u0085cd"}',
        "reason RLO": '{"start_s": 0, "end_s": 1.4, "reason": "ab\\u202ecd"}',
        "reason 501": '{"start_s": 0, "end_s": 1.5, "reason": "' + "가" * 501 + '"}',
        "reason 500": '{"start_s": 0, "end_s": 1.6, "reason": "' + "가" * 500 + '"}',
        "reason 2 spaces": '{"start_s": 0, "end_s": 1.7, "reason": "  "}',
        "reason <script>": '{"start_s": 0, "end_s": 1.8, "reason": "<script>alert(1)</script>"}',
    }
    created = []
    for name, raw in cases.items():
        r = ctx.client.post(path, content=raw, headers={**h, "Content-Type": "application/json"})
        obs("fuzz", name, "->", r.status_code, (r.json().get("job_id") if r.status_code == 202 else
                                                 [e.get("code") for e in r.json().get("errors", [])] or r.json().get("code")))
        if r.status_code == 202:
            created.append(name)
        # free the pending slot so the cap does not mask results
        with ctx.c.engine.begin() as conn:
            conn.execute(text("UPDATE jobs SET status='DONE' WHERE job_type='clip_export' AND status='QUEUED'"))
    with ctx.c.engine.connect() as conn:
        rows = conn.execute(text("SELECT payload->>'start_s', payload->>'end_s' FROM jobs WHERE job_type='clip_export'")).all()
    obs("stored payload ranges", [tuple(x) for x in rows])
    with ctx.c.engine.connect() as conn:
        rows = conn.execute(text("SELECT reason, details FROM audit_log WHERE action='CLIP_EXPORT_REQUESTED'")).all()
    obs("audit reasons", [(repr(r[0])[:40], r[1]) for r in rows][:6])


def test_pending_cap_under_concurrency(ictx, tmp_path):
    ctx = ictx
    src = _source(ctx, video(tmp_path, "testsrc2=size=320x240:rate=15", "a.mp4"))
    src2 = _source(ctx, video(tmp_path, "testsrc2=size=320x240:rate=15,hue=s=0", "b.mp4"))
    _set_room(ctx, 10**9)
    ctx.c.settings.__dict__["clip_max_pending_per_org"] = 3
    h = ctx.as_user(MEMBER_A)

    def post(i):
        asset = src if i % 2 == 0 else src2
        return ctx.client.post(CLIPS.format(asset_id=asset), json={"start_s": 0, "end_s": 1 + i * 0.01}, headers=h)

    with ThreadPoolExecutor(max_workers=10) as pool:
        rs = list(pool.map(post, range(10)))
    obs("concurrent 10 posts (cap 3):", sorted(r.status_code for r in rs), "jobs", len(_clip_jobs(ctx)),
        "audits", sum(1 for a in audit_rows(ctx) if a[0] == "CLIP_EXPORT_REQUESTED"))
    # same range concurrently on same asset
    with ctx.c.engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET status='DONE' WHERE job_type='clip_export'"))

    def same(_):
        return ctx.client.post(CLIPS.format(asset_id=src), json={"start_s": 2.0, "end_s": 3.0}, headers=h)

    with ThreadPoolExecutor(max_workers=6) as pool:
        rs = list(pool.map(same, range(6)))
    ids = {r.json().get("job_id") for r in rs if r.status_code == 202}
    obs("concurrent same-range:", sorted(r.status_code for r in rs), "distinct job ids", len(ids))


def test_stream_metadata_survives_in_clip(ictx, tmp_path):
    from app.services.clips import clip_args
    s = ictx.c.settings
    src = tmp_path / "tagged.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=160x120:rate=15", "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-metadata", "title=GLOBAL-SECRET", "-metadata:s:v:0", "comment=GPS+35.1234+129.5678",
                    "-metadata:s:v:0", "handler_name=cam-op-Kim", "-metadata:s:v:0", "creation_time=2026-11-01T01:00:00Z",
                    str(src)], check=True)
    out = tmp_path / "clip.mp4"
    subprocess.run(clip_args(s, str(src), str(out), 0.0, 1.0), check=True, capture_output=True)
    pr = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(out)],
                        capture_output=True, check=True)
    j = json.loads(pr.stdout)
    obs("clip format tags", j["format"].get("tags"))
    obs("clip video stream tags", [st.get("tags") for st in j["streams"] if st["codec_type"] == "video"])


def test_clip_of_unknown_timestamp_parent_vs_contract(ictx, tmp_path):
    ctx = ictx
    from tests.acceptance.test_t09_deid_review import original
    src = original(ctx, video(tmp_path, "testsrc2=size=320x240:rate=15", "u.mp4"))
    with ctx.c.sessionmaker.begin() as db:
        a = db.get(Asset, src)
        a.capture_start_at = None
        a.timestamp_quality = {"source": "UNKNOWN", "verified": False, "est_offset_ms": None}
        a.quality_status, a.quality_reasons = "WARNING", ["capture_time_unknown"]
        a.location_precision = "GENERALIZED"
    r = ctx.client.post(CLIPS.format(asset_id=src), json={"start_s": 0, "end_s": 1}, headers=ctx.as_user(MEMBER_A))
    assert r.status_code == 202, r.text
    drain(ctx)
    with ctx.c.sessionmaker() as db:
        cid = db.execute(select(Asset.asset_id).where(Asset.variant == "CLIP")).scalar_one()
        c = db.get(Asset, cid)
        obs("unknown-ts parent -> clip: capture_start", c.capture_start_at, "ts_quality", c.timestamp_quality,
            "quality", c.quality_status, c.quality_reasons)
    g = ctx.client.get(f"/v1/assets/{cid}", headers=ctx.as_user(MEMBER_A))
    try:
        check(g, "GET", "/v1/assets/{asset_id}")
        obs("contract check of clip metadata: OK")
    except AssertionError as e:
        obs("contract check of clip metadata FAILED:", str(e)[:300])


def test_duplicate_run_same_job_twice(ictx, tmp_path):
    from app.services import clips
    ctx = ictx
    src = _source(ctx, video(tmp_path, "testsrc2=size=320x240:rate=15", "a.mp4"))
    r = ctx.client.post(CLIPS.format(asset_id=src), json={"start_s": 1, "end_s": 2}, headers=ctx.as_user(MEMBER_A))
    job_id = r.json()["job_id"]
    payload = {"asset_id": src, "start_s": 1.0, "end_s": 2.0}
    res = {}

    def run(n):
        try:
            res[n] = clips.make_clip(ctx.c, job_id, payload)
        except Exception as exc:  # noqa: BLE001
            res[n] = f"EXC {type(exc).__name__}: {exc}"

    ts = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    [t.start() for t in ts]; [t.join(120) for t in ts]
    with ctx.c.sessionmaker() as db:
        n = len(db.execute(select(Asset).where(Asset.variant == "CLIP")).all())
        used = db.get(OrgQuota, ORG_A).used_bytes
    objs = list(ctx.c.storage.list_objects(ctx.c.settings.bucket_proc, ORG_A + "/"))
    obs("two concurrent runs of the same job:", res, "clip assets", n, "proc objects", [o.key[-40:] for o in objs],
        "used_bytes", used)


def test_subframe_range_outcome(ictx, tmp_path):
    ctx = ictx
    src = _source(ctx, video(tmp_path, "testsrc2=size=320x240:rate=15", "a.mp4"))
    r = ctx.client.post(CLIPS.format(asset_id=src), json={"start_s": 1.0, "end_s": 1.001}, headers=ctx.as_user(MEMBER_A))
    obs("subframe POST", r.status_code)
    drain(ctx)
    obs("subframe job", _job(ctx, r.json()["job_id"]))
    with ctx.c.sessionmaker() as db:
        rows = db.execute(select(Asset.asset_id, Asset.byte_size, Asset.ext, Asset.quality_status).where(Asset.variant == "CLIP")).all()
        obs("subframe clip assets", [(x[1], x[2], x[3]) for x in rows])
    g = ctx.client.get(CLIPS.format(asset_id=src), headers=ctx.as_user(MEMBER_A)).json()["items"]
    obs("GET items", g)
