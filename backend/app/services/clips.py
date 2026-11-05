"""구간 클립 내보내기 (09번 요구사항 7.2, ADR-0033).

- 요청은 원 영상·구간을 검사하고 작업(clip_export)만 등록한다 (202).
  같은 구간 작업이 대기·실행 중이면 그 작업을 돌려준다.
- 작업은 입력 앞 -ss + 재인코딩(libx264)으로 프레임 단위로 정확히 자른다. 결과는 create_derived로 원 영상의 파생 자산
  (variant=CLIP, PRIVATE, 계보 relation=CLIP)이 되고 provenance.clip_range_s와 옮긴 촬영 시각·video_ext를 남긴다.
- 다시 해도 같은 실패(원 영상이 격리·삭제됨, 인코딩 실패)는 재시도하지 않고 바로 FAILED (PermanentJobError).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.container import Container
from app.db.models import Asset, AssetLineage
from app.errors import ProblemError
from app.ports.queue import PermanentJobError
from app.timefmt import iso

JOB_TYPE = "clip_export"
SOURCE_VARIANTS = ("ORIGINAL", "CLIP")
SOURCE_STATUSES = ("REGISTERED", "PROXIED", "READY_PRIVATE")
ACTIVE = ("QUEUED", "RUNNING")


def _sec(x: float) -> float:
    return round(float(x), 3)  # 밀리초 단위로 맞춘다 (같은 구간 판단·표시)


def source_duration(a: Asset) -> float:
    """잘라낼 수 있는 원 영상인지 확인하고 길이(초)를 돌려준다. 안 되면 409."""
    if not a.has_binary or a.data_type != "VIDEO" or a.variant not in SOURCE_VARIANTS or a.deleted_at is not None:
        raise ProblemError("CONFLICT", detail="바이너리가 있는 원 영상(ORIGINAL·CLIP)만 자를 수 있습니다")
    if a.ingest_status not in SOURCE_STATUSES:
        raise ProblemError("CONFLICT", detail=f"{a.ingest_status} 상태의 영상은 자를 수 없습니다")
    duration = ((a.ext or {}).get("video_ext") or {}).get("duration_s")
    if not duration:
        raise ProblemError("CONFLICT", detail="영상 길이를 아직 모릅니다. 메타 추출이 끝난 뒤 다시 요청하세요")
    return float(duration)


def check_range(c: Container, start_s: float, end_s: float, duration_s: float) -> tuple[float, float]:
    start, end = _sec(start_s), _sec(end_s)
    errors: list[dict[str, str]] = []
    if end <= start:
        errors.append({"pointer": "/end_s", "code": "range", "message": "끝은 시작보다 뒤여야 합니다"})
    elif end - start > c.settings.clip_max_s:
        errors.append({"pointer": "/end_s", "code": "max_length",
                       "message": f"클립은 최대 {c.settings.clip_max_s:g}초까지 만들 수 있습니다"})
    if end > _sec(duration_s):
        errors.append({"pointer": "/end_s", "code": "beyond_duration",
                       "message": f"영상 길이({_sec(duration_s):g}초)를 넘습니다"})
    if errors:
        raise ProblemError("VALIDATION_ERROR", errors=errors)
    return start, end


def request_clip(c: Container, a: Asset, requested_by: str, start_s: float,
                 end_s: float) -> tuple[dict[str, Any], bool]:
    """작업을 등록한다. (응답, 새로 등록했는지). 같은 구간 작업이 대기·실행 중이면 그 작업을 돌려준다."""
    start, end = check_range(c, start_s, end_s, source_duration(a))
    for j in c.queue.jobs_for_asset(JOB_TYPE, a.asset_id):
        if j.status in ACTIVE and (_sec(j.payload["start_s"]), _sec(j.payload["end_s"])) == (start, end):
            return {"job_id": j.job_id, "asset_id": a.asset_id, "start_s": start, "end_s": end, "status": j.status,
                    "requested_at": iso(j.created_at)}, False
    now = c.clock.now()
    job_id = c.queue.enqueue(JOB_TYPE, {"asset_id": a.asset_id, "start_s": start, "end_s": end,
                                        "requested_by": requested_by}, organization_id=a.organization_id)
    return {"job_id": job_id, "asset_id": a.asset_id, "start_s": start, "end_s": end, "status": "QUEUED",
            "requested_at": iso(now)}, True


def list_clips(c: Container, db: Session, a: Asset) -> list[dict[str, Any]]:
    """이 영상의 클립 작업 (최근 요청부터). DONE이면 결과 자산을 붙인다 (지워졌으면 없음)."""
    rows = db.execute(select(Asset.asset_id, Asset.provenance)
                      .join(AssetLineage, AssetLineage.child_asset_id == Asset.asset_id)
                      .where(AssetLineage.parent_asset_id == a.asset_id, AssetLineage.relation == "CLIP",
                             Asset.deleted_at.is_(None))).all()
    by_job: dict[str, str] = {}
    by_range: dict[tuple[float, float], str] = {}
    for aid, prov in rows:
        prov = prov or {}
        if prov.get("job_id"):
            by_job[prov["job_id"]] = aid
        rng = prov.get("clip_range_s")
        if rng and len(rng) == 2:
            by_range.setdefault((_sec(rng[0]), _sec(rng[1])), aid)
    out: list[dict[str, Any]] = []
    for j in c.queue.jobs_for_asset(JOB_TYPE, a.asset_id):
        start, end = _sec(j.payload["start_s"]), _sec(j.payload["end_s"])
        item: dict[str, Any] = {"job_id": j.job_id, "asset_id": a.asset_id, "start_s": start, "end_s": end,
                                "status": j.status, "requested_at": iso(j.created_at)}
        if j.status == "DONE":
            # 같은 결과(같은 체크섬)가 이미 있으면 create_derived가 기존 자산을 돌려주므로 구간으로도 찾는다
            clip = by_job.get(j.job_id) or by_range.get((start, end))
            if clip:
                item["clip_asset_id"] = clip
        elif j.status == "FAILED" and j.last_error_code:
            item["error_code"] = j.last_error_code
        out.append(item)
    return out


def clip_args(s: Any, src: str, out: str, start_s: float, end_s: float) -> list[str]:
    """입력 앞 -ss는 가까운 키프레임부터 디코딩해 시작 시각 전 프레임을 버린다 → 재인코딩하면 프레임 단위로 정확하다."""
    return [s.ffmpeg_path, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start_s:.3f}", "-i", src,
            "-t", f"{end_s - start_s:.3f}", "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", str(int(s.clip_crf)), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-map_metadata", "-1",
            "-movflags", "+faststart", out]


def _usable(a: Asset | None) -> bool:
    return a is not None and a.deleted_at is None and a.has_binary and a.ingest_status in SOURCE_STATUSES


def make_clip(c: Container, job_id: str, payload: dict[str, Any]) -> str:
    from app.services.pipeline import _run, create_derived, local_copy, parse_probe, tool_version, work_dir

    asset_id, start, end = payload["asset_id"], _sec(payload["start_s"]), _sec(payload["end_s"])
    s = c.settings
    with c.sessionmaker() as db:
        parent = db.get(Asset, asset_id)
        if not _usable(parent):  # 요청 뒤 격리·삭제됨 — 다시 해도 같다
            raise PermanentJobError("SOURCE_UNAVAILABLE")
        assert parent is not None
        db.expunge(parent)
    with local_copy(c, parent) as src, tempfile.TemporaryDirectory(prefix="marine-clip-", dir=work_dir(c)) as tmp:
        out = os.path.join(tmp, "clip.mp4")
        r = _run(clip_args(s, src, out, start, end), s.ffmpeg_timeout_s)
        if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
            raise PermanentJobError("CLIP_ENCODE_FAILED")
        video_ext: dict[str, Any] = {}
        pr = _run([s.ffprobe_path, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", out],
                  s.ffmpeg_timeout_s)
        if pr.returncode == 0:
            try:
                video_ext = parse_probe(json.loads(pr.stdout or b"{}"))["video_ext"]
            except Exception:  # noqa: BLE001 - 길이 정보가 없어도 클립 자체는 유효하다
                video_ext = {}
        with c.sessionmaker.begin() as db:
            p = db.get(Asset, asset_id)
            if not _usable(p):
                raise PermanentJobError("SOURCE_UNAVAILABLE")
            assert p is not None
            overrides: dict[str, Any] = {"ext": {"video_ext": video_ext} if video_ext else {}}
            if p.capture_start_at is not None:  # 촬영 시각은 원 영상 시작 + 구간 (미상이면 미상 그대로)
                overrides["capture_start_at"] = p.capture_start_at + timedelta(seconds=start)
                overrides["capture_end_at"] = p.capture_start_at + timedelta(seconds=end)
            create_derived(c, db, p, out, variant="CLIP", mime="video/mp4", relation="CLIP",
                           tool=f"{tool_version(s.ffmpeg_path)} libx264 crf{int(s.clip_crf)}", job_id=job_id,
                           provenance_extra={"clip_range_s": [start, end]}, overrides=overrides)
    return "OK"
