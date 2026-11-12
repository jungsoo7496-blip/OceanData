"""검토 2026-11-12 C7: 클립 작업이 워커를 오래 쥐지 않게 (ADR-0069).

- 시간 한도: 클립의 ffmpeg 한도는 구간 길이 × CLIP_TIMEOUT_FACTOR(최소 CLIP_TIMEOUT_MIN_S, 최대 FFMPEG_TIMEOUT_S)다.
  넘으면 ffmpeg를 끝내고 재시도 없이 FAILED(CLIP_TIMEOUT) — 클립 목록에 원인 코드가 보인다.
- 전용 워커: WORKER_JOB_TYPES·WORKER_EXCLUDE_JOB_TYPES로 워커가 가져갈 작업 유형을 나눈다(기본은 모든 유형).
- 임대: 클립 작업이 도는 동안 워커가 임대를 늘려, 임대보다 오래 걸려도 다른 워커가 회수해 두 번 돌리지 않는다.
- 우선순위: 대기 중인 수집 작업(무결성 검증·메타 추출·프록시·썸네일)은 대기 중인 클립보다 먼저 배정된다.

합성 영상(ffmpeg testsrc2)과 잠만 자는 가짜 ffmpeg 스크립트만 쓴다.
"""

from __future__ import annotations

import os
import shutil
import stat
import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from app.seed import ORG_A
from app.services import clips, pipeline
from app.workers import main as worker
from tests.acceptance.test_t09_deid_review import video
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_clips import MEMBER_A, _job, _no_clip_assets, _source
from tests.integration.test_pipeline import drain

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg가 필요합니다")]


def _post_clip(ctx, asset_id: str, start: float, end: float) -> str:  # type: ignore[no-untyped-def]
    r = ctx.client.post(f"/v1/assets/{asset_id}/clips", json={"start_s": start, "end_s": end},
                        headers=ctx.as_user(MEMBER_A))
    assert r.status_code == 202, r.text
    return str(r.json()["job_id"])


def test_clip_ffmpeg_limit_follows_clip_length(ictx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """한도 = 구간 길이 × 배수, 아래는 최소값·위는 전체 한도로 자른다 (짧은 클립이 한 시간 한도를 받지 않는다)."""
    ctx = ictx
    src = _source(ctx, video(tmp_path, "testsrc2=size=320x240:rate=15", "a.mp4"))
    s = ctx.c.settings.__dict__
    s["clip_timeout_factor"], s["clip_timeout_min_s"], s["ffmpeg_timeout_s"] = 10.0, 12, 35
    calls: list[tuple[str, bool, int]] = []
    real_run = pipeline._run

    def spy(args: list[str], timeout_s: int) -> Any:
        calls.append((os.path.basename(args[0]), "-ss" in args, timeout_s))
        return real_run(args, timeout_s)

    monkeypatch.setattr(pipeline, "_run", spy)
    jobs = [_post_clip(ctx, src, 0, 1.0), _post_clip(ctx, src, 0, 2.5), _post_clip(ctx, src, 0, 4.0)]
    drain(ctx)
    assert [_job(ctx, j)[0] for j in jobs] == ["DONE", "DONE", "DONE"]
    encode = sorted(t for tool, cut, t in calls if tool == "ffmpeg" and cut)
    assert encode == [12, 25, 35]  # 1초 → 최소 12초, 2.5초 → 25초, 4초 → 40초지만 전체 한도 35초
    probe = sorted(t for tool, _, t in calls if tool == "ffprobe")
    assert probe == [12, 25, 35]  # 결과 다시 읽기도 같은 한도 (전체 한도 3600초가 아니다)


def test_clip_timeout_kills_ffmpeg_and_fails_without_retry(ictx, tmp_path: Path) -> None:
    """한도를 넘긴 ffmpeg는 끝내고 작업은 시도 1번에 FAILED(CLIP_TIMEOUT). 클립 자산·임시 파일은 남지 않는다."""
    ctx = ictx
    src = _source(ctx, video(tmp_path, "testsrc2=size=320x240:rate=15", "a.mp4"))
    pid_file = tmp_path / "ffmpeg.pid"
    fake = tmp_path / "slow-ffmpeg.sh"  # 멈춘 인코딩 대역: 자기 PID를 남기고 20초 잔다
    fake.write_text(f"#!/bin/sh\necho $$ > {pid_file}\nexec sleep 20\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    work = tmp_path / "work"
    s = ctx.c.settings.__dict__
    s["ffmpeg_path"], s["clip_timeout_min_s"], s["clip_timeout_factor"], s["work_dir"] = str(fake), 1, 0.5, str(work)
    job_id = _post_clip(ctx, src, 0, 1.0)
    started = time.monotonic()
    drain(ctx)
    assert time.monotonic() - started < 10  # 전체 한도(3600초)나 스크립트의 20초를 기다리지 않는다
    assert _job(ctx, job_id) == ("FAILED", 1, "CLIP_TIMEOUT")
    r = ctx.client.get(f"/v1/assets/{src}/clips", headers=ctx.as_user(MEMBER_A))
    check(r, "GET", "/v1/assets/{asset_id}/clips")
    assert [(x["status"], x.get("error_code"), x.get("clip_asset_id")) for x in r.json()["items"]] == [
        ("FAILED", "CLIP_TIMEOUT", None)]
    _no_clip_assets(ctx)
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):  # 시간 한도에서 ffmpeg 프로세스를 끝냈다
        os.kill(pid, 0)
    assert [p for p in os.listdir(work) if p.startswith("marine-")] == []  # 받아 둔 원본·임시 폴더도 지웠다


def test_dedicated_clip_worker_split(ctx, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """기본 워커는 클립을 빼고, 전용 워커는 클립만 가져간다. 잘못된 설정은 기동할 때 막는다."""
    c, s = ctx.c, ctx.c.settings.__dict__
    clip = c.queue.enqueue(clips.JOB_TYPE, {"asset_id": "ast_none", "start_s": 0, "end_s": 1}, organization_id=ORG_A)
    noop = c.queue.enqueue("noop", {})
    s["worker_exclude_job_types"] = [clips.JOB_TYPE]  # 기본 워커: 클립 빼고
    assert worker.run_once(c, "main") and _job(ctx, noop)[0] == "DONE"
    assert not worker.run_once(c, "main")  # 대기 중인 클립은 가져가지 않는다
    assert _job(ctx, clip) == ("QUEUED", 0, None)
    s["worker_exclude_job_types"], s["worker_job_types"] = [], [clips.JOB_TYPE]  # 전용 워커: 클립만
    noop2 = c.queue.enqueue("noop", {})
    assert worker.run_once(c, "clips")
    assert _job(ctx, clip)[0] == "FAILED"  # 가져가 돌렸다 (원 영상이 없어 SOURCE_UNAVAILABLE)
    assert not worker.run_once(c, "clips") and _job(ctx, noop2) == ("QUEUED", 0, None)
    s["worker_job_types"] = []
    assert worker.claim_types(c.settings) == sorted(worker.HANDLERS)  # 기본: 등록된 모든 유형 (지금과 같다)
    for include, exclude in (([clips.JOB_TYPE], [clips.JOB_TYPE]), (["clip_exprot"], []), ([], ["no_such_job"])):
        s["worker_job_types"], s["worker_exclude_job_types"] = include, exclude
        with pytest.raises(ValueError):  # 빈 목록(모든 유형으로 오해)·없는 유형 이름(오타)
            worker.claim_types(c.settings)
    from tests.conftest import test_settings

    # Compose·.env.example이 넘기는 모양 그대로 (빈 값 = 모든 유형, 쉼표 구분)
    for key, value in (("WORKER_JOB_TYPES", "clip_export, noop"), ("WORKER_EXCLUDE_JOB_TYPES", ""),
                       ("CLIP_TIMEOUT_FACTOR", "4"), ("CLIP_TIMEOUT_MIN_S", "120")):
        monkeypatch.setenv(key, value)
    parsed = test_settings()
    assert (parsed.worker_job_types, parsed.worker_exclude_job_types) == (["clip_export", "noop"], [])
    assert (parsed.clip_timeout_factor, parsed.clip_timeout_min_s) == (4.0, 120)
    assert clips.clip_timeout_s(parsed, 30) == 120 and clips.clip_timeout_s(parsed, 3600) == 3600  # 기본값


def test_long_clip_job_keeps_its_lease(ctx, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """클립이 임대(여기서는 3초)보다 오래 걸려도, 그동안 다른 워커의 임대 회수가 작업을 대기로 돌려 다시 배정하지 않는다."""
    c = ctx.c
    with c.engine.begin() as conn:
        conn.execute(text("UPDATE job_policies SET lease_s = 3 WHERE job_type = 'clip_export'"))
    job_id = c.queue.enqueue(clips.JOB_TYPE, {"asset_id": "ast_none", "start_s": 0, "end_s": 1}, organization_id=ORG_A)
    seen: dict[str, Any] = {"reaped": 0, "stolen": [], "runs": 0}

    def slow_clip(c_: Any, jid: str, payload: dict[str, Any]) -> str:
        seen["runs"] += 1
        deadline = time.monotonic() + 5.5  # 임대 3초를 넘겨 돈다. 그동안 다른 워커가 주기 정리·배정을 한다
        while time.monotonic() < deadline:
            seen["reaped"] += c.queue.reap_expired_leases()
            other = c.queue.claim([clips.JOB_TYPE], "other-worker")
            if other is not None:
                seen["stolen"].append(other.job_id)
            time.sleep(0.2)
        return "OK"

    monkeypatch.setattr(clips, "make_clip", slow_clip)
    assert worker.run_once(c, "clip-worker")
    assert (seen["reaped"], seen["stolen"], seen["runs"]) == (0, [], 1)
    assert _job(ctx, job_id) == ("DONE", 1, None)
    with c.engine.connect() as conn:  # 끝난 작업에는 임대가 남지 않는다 (갱신 스레드가 끝난 뒤 다시 걸지 않았다)
        assert conn.execute(text("SELECT lease_expires_at FROM jobs WHERE job_id = :j"), {"j": job_id}).scalar() is None

    # 임대를 늘릴 수 있는 것은 지금 작업을 쥔 워커뿐이다 (회수된 뒤 다른 워커가 가져간 작업은 늘리지 못한다)
    noop = c.queue.enqueue("noop", {})
    assert c.queue.claim(["noop"], "w1") is not None
    with c.engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE job_id = :j"),
                     {"j": noop})
    assert c.queue.reap_expired_leases() == 1
    assert c.queue.claim(["noop"], "w2") is not None
    assert c.queue.renew_lease(noop, "w1", 600) is False
    assert c.queue.renew_lease(noop, "w2", 600) is True


def test_ingestion_jobs_are_claimed_before_queued_clips(ctx) -> None:  # type: ignore[no-untyped-def]
    """정책 표 우선순위: 먼저 들어온 클립이 있어도 무결성 검증·메타 추출·프록시·썸네일을 먼저 배정한다 (0021: 썸네일 다음)."""
    c = ctx.c
    c.queue.enqueue(clips.JOB_TYPE, {"asset_id": "ast_none", "start_s": 0, "end_s": 1}, organization_id=ORG_A)
    for job_type in ("make_thumbnail", "make_proxy", "media_probe", "integrity_verify"):
        c.queue.enqueue(job_type, {"asset_id": None})
    order = []
    while (job := c.queue.claim(sorted(worker.HANDLERS), "w")) is not None:
        order.append(job.job_type)
    assert order == ["integrity_verify", "media_probe", "make_proxy", "make_thumbnail", clips.JOB_TYPE]
