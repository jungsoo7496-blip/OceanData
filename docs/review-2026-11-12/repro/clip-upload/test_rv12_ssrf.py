"""RV12 scratch (adjacent finding): text/HLS file accepted as VIDEO -> ffprobe/ffmpeg follow URLs from the worker."""
from __future__ import annotations

import shutil
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from sqlalchemy import text

from app.db.models import Asset
from app.workers import main as worker
from tests.integration.test_ingest import manifest, upload_all

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe")]


def test_hls_playlist_declared_as_video_makes_worker_fetch_url(ictx):
    hits = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            self.send_response(404)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    playlist = (f"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:4,\n"
                f"http://127.0.0.1:{port}/internal-metadata-probe\n#EXT-X-ENDLIST\n").encode()
    h = ictx.as_user(102)
    m = manifest(playlist, data_type="VIDEO", variant="CLIP", mime="text/plain")
    created = upload_all(ictx, m, playlist, h)
    while worker.run_once(ictx.c, "t"):
        pass
    with ictx.c.sessionmaker() as db:
        a = db.get(Asset, created["asset_id"])
        print("OBS asset", a.data_type, a.variant, a.ingest_status, a.quarantine_reason, a.mime_type)
    with ictx.c.engine.connect() as conn:
        print("OBS jobs", [tuple(r) for r in conn.execute(text("SELECT job_type,status,last_error_code FROM jobs ORDER BY created_at"))])
    print("OBS worker-side ffprobe/ffmpeg requested URLs on the attacker-controlled host:", hits)
    srv.shutdown()
