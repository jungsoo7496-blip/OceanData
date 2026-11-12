import pytest
import rv12_probe_plugin as P
from test_rv12_timing import flip_to_request_scope

pytestmark = pytest.mark.integration


def test_probe_detects_old_behaviour(ctx):
    P.SUMMARY.clear(); P.RECORDS.clear()
    assert ctx.client.get("/v1/me", headers=ctx.as_user(102)).status_code == 200
    print("PROBE new:", dict(P.SUMMARY), P.RECORDS)
    assert P.SUMMARY["late_commit"] == 0
    P.SUMMARY.clear(); P.RECORDS.clear()
    n = flip_to_request_scope(ctx.client.app)
    assert ctx.client.get("/v1/me", headers=ctx.as_user(102)).status_code == 200
    print("PROBE flipped(%d):" % n, dict(P.SUMMARY), P.RECORDS)
    assert P.SUMMARY["late_commit"] == 1
