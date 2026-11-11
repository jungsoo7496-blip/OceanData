"""운영자 웹훅 수신 지연이 워커 작업 처리와 장애 기록을 막지 않는다."""

from __future__ import annotations

import dataclasses
import json
import signal
import socket
import threading
import time
from collections.abc import Callable
from types import SimpleNamespace

import pytest
from sqlalchemy import update

from app.db.models import Incident, JobRow, WatchRule
from app.workers import main as worker
from tests.conftest import Ctx
from tests.unit.test_webhook_send import SyntheticReceiver, read_request

pytestmark = pytest.mark.integration


def test_worker_finishes_job_while_operator_webhook_holds_headers(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch,
) -> None:
    rule = "HARDWARE_SIGNAL"
    with ctx.c.sessionmaker.begin() as db:
        db.execute(update(WatchRule).values(enabled=False))
        db.execute(update(WatchRule).where(WatchRule.rule_code == rule)
                   .values(enabled=True, channels=["WEBHOOK"], notify_operator=True))
    signal_body = {
        "host": "synthetic-storage", "kind": "TEMPERATURE", "component": "cpu0", "value": 92.5,
        "unit": "degC", "status": "CRITICAL", "observed_at": ctx.c.clock.now().isoformat(),
    }
    response = ctx.client.post("/v1/hardware-signals", json={"signals": [signal_body]}, headers=ctx.as_user(106))
    assert response.status_code == 202, response.text

    request_received = threading.Event()
    release_headers = threading.Event()

    def hold_headers(conn: socket.socket, seen: list[bytes]) -> None:
        conn.settimeout(5)
        seen.append(read_request(conn))
        conn.sendall(b"HTTP/1.1 204 No Content\r\nX-Pending: ")
        request_received.set()
        if release_headers.wait(10):
            conn.sendall(b"done\r\nContent-Length: 0\r\n\r\n")

    receiver = SyntheticReceiver(hold_headers)
    settings = ctx.c.settings.model_copy(update={
        "alert_webhook_url": f"http://127.0.0.1:{receiver.port}/alerts",
        "watch_eval_interval_s": 1,
        "worker_poll_interval_s": 0.01,
    })
    c = dataclasses.replace(ctx.c, settings=settings)
    stoppers: dict[int, Callable[..., None]] = {}
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setattr(worker, "signal", SimpleNamespace(
        SIGTERM=signal.SIGTERM, SIGINT=signal.SIGINT, signal=stoppers.__setitem__,
    ))
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "build_container", lambda _settings: c)
    for name in ("configure_logging", "start_http_server", "sweep", "maintenance"):
        monkeypatch.setattr(worker, name, lambda *_args, **_kwargs: None)
    job_type = "test_operator_webhook_isolation"
    monkeypatch.setitem(worker.HANDLERS, job_type, worker.Handler(lambda _c, _job: None))

    thread = threading.Thread(target=worker.main, daemon=True)
    thread.start()
    try:
        assert request_received.wait(5), "합성 수신기가 운영자 웹훅 요청을 받지 못했습니다"
        payload = json.loads(receiver.seen[0].partition(b"\r\n\r\n")[2])
        assert (payload["type"], payload["rule_code"]) == ("watch.opened", rule)

        job_id = c.queue.enqueue(job_type, {})
        deadline = time.monotonic() + 2
        with c.sessionmaker() as db:
            while True:
                job = db.get_one(JobRow, job_id, populate_existing=True)
                if job.status == "DONE" or time.monotonic() >= deadline:
                    break
                time.sleep(0.025)
            incident = db.get(Incident, payload["incident_id"])
            assert (job.status, incident is not None) == ("DONE", True), "응답 헤더 보류 중 작업 완료 및 장애 커밋"
    finally:
        stop = stoppers.get(signal.SIGTERM)
        if stop is not None:
            stop()
        release_headers.set()
        thread.join(timeout=10)
        receiver.close()
        assert not thread.is_alive(), "워커가 종료되지 않았습니다"
