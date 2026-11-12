"""rv12 (3) with a *real* COMMIT delay: deferred constraint trigger that pg_sleeps at COMMIT time."""
from __future__ import annotations

import json
import time

import pytest
from sqlalchemy import text

from rv12_live import LiveServer, raw_request
from test_rv12_timing import EVENT, flip_to_request_scope

pytestmark = pytest.mark.integration


def test_db_level_commit_delay(ctx):
    with ctx.c.engine.begin() as conn:
        conn.execute(text("CREATE OR REPLACE FUNCTION rv12_slow_commit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(1.5); RETURN NULL; END $$"))
        conn.execute(text("CREATE CONSTRAINT TRIGGER rv12_slow AFTER INSERT ON events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION rv12_slow_commit()"))
    srv = LiveServer(ctx.client.app).start()
    try:
        hdr = {**ctx.as_user(102), "Content-Type": "application/json"}
        body = json.dumps(EVENT).encode()
        r = raw_request(srv.port, "POST", "/v1/events", hdr, body)
        print(f"DBCOMMIT new : status={r['status']} first_byte_after_send={(r['t_first'] - r['t_sent']) * 1000:.0f} ms (trigger sleeps 1500 ms at COMMIT)")
        flip_to_request_scope(ctx.client.app)
        r = raw_request(srv.port, "POST", "/v1/events", hdr, body)
        print(f"DBCOMMIT old : status={r['status']} first_byte_after_send={(r['t_first'] - r['t_sent']) * 1000:.0f} ms")
        time.sleep(2.0)
    finally:
        srv.stop()
