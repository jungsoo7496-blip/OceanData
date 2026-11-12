"""Scratch verification 2 (read-only review; not part of repo)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

OUT = "/tmp/claude-0/-home-user-oceandata/804cf8c5-3e8a-5d79-bc09-75a711922000/scratchpad/sweep/status-events-observations/out2.txt"


def log(label: str, value: object) -> None:
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(f"[{label}] {value}\n")


def test_more(ctx) -> None:
    op = ctx.as_user(106)
    r = ctx.client.get("/v1/usage", headers=op)
    log("usage.operator.fresh", (r.status_code, r.json()))
    log("usage.provider.fresh", ctx.client.get("/v1/usage", headers=ctx.as_user(103)).json())

    src = {"source_id": "khoa-test", "name": "합성 소스", "adapter_kind": "MOCK", "interval_s": 600,
           "stations": [{"code": "S1", "name": "합성", "lat": 34.7, "lon": 126.5}],
           "field_mapping": {"station_path": "$.s", "time_path": "$.t",
                             "metrics": {"sea_water_temperature": {"path": "$.v", "unit": "degC"}}},
           "attribution": "합성 출처", "redistribution_allowed": False}
    log("external.create", ctx.client.post("/v1/external-sources", json=src, headers=op).status_code)
    r = ctx.client.patch("/v1/external-sources/khoa-test", json={"enabled": False, "interval_s": None}, headers=op)
    log("external.patch.combined.response", (r.status_code, r.json().get("enabled"), r.json().get("interval_s")))
    cur = [s for s in ctx.client.get("/v1/external-sources", headers=op).json()["items"]
           if s["source_id"] == "khoa-test"][0]
    log("external.patch.combined.persisted", (cur["enabled"], cur["interval_s"]))
    with ctx.c.engine.connect() as conn:
        log("external.audit", [r[0] for r in conn.execute(text(
            "SELECT action FROM audit_log WHERE action LIKE 'EXTERNAL_SOURCE%' ORDER BY seq"))])

    now = datetime.now(UTC).replace(microsecond=0)
    body = {"from": (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    r1 = ctx.client.post("/v1/external-sources/khoa-test/backfill", json=body, headers=op)
    with ctx.c.engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET status='FAILED' WHERE job_type='collect_source'"))
        n1 = conn.execute(text("SELECT count(*) FROM jobs WHERE job_type='collect_source'")).scalar()
    r2 = ctx.client.post("/v1/external-sources/khoa-test/backfill", json=body, headers=op)
    with ctx.c.engine.connect() as conn:
        n2 = conn.execute(text("SELECT count(*) FROM jobs WHERE job_type='collect_source'")).scalar()
        q2 = conn.execute(text("SELECT count(*) FROM jobs WHERE job_type='collect_source' AND status='QUEUED'")).scalar()
    log("backfill.repeat", (r1.status_code, r1.json(), "jobs_rows_after_first", n1, r2.status_code, r2.json(),
                            "jobs_rows_after_second", n2, "queued_after_second", q2))
