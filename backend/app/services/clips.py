"""구간 클립 내보내기 (09번 요구사항 7.2, ADR-0033).

- 요청은 원 영상·구간을 검사하고 작업(clip_export)만 등록한다 (202).
  같은 구간 작업이 대기·실행 중이면 그 작업을 돌려준다. 새로 등록할 때는 기관 쿼터에 여유가 있어야 하고(업로드와 같은
  규칙, 409 QUOTA_EXCEEDED) 기관의 대기·실행 중 클립 작업이 CLIP_MAX_PENDING_PER_ORG 미만이어야 한다(429 RATE_LIMITED).
  작업 행은 호출자 트랜잭션에 넣어 감사(CLIP_EXPORT_REQUESTED)와 함께 확정되거나 함께 취소된다.
- 작업은 입력 앞 -ss + 재인코딩(libx264)으로 프레임 단위로 정확히 자른다. 결과는 원 영상의 파생 자산
  (variant=CLIP, PRIVATE, 계보 relation=CLIP)이 되고 provenance.clip_range_s와 옮긴 촬영 시각·video_ext를 남긴다.
  객체는 잠그지 않고 먼저 올린 뒤, 원 영상 행을 잠근(FOR UPDATE) 트랜잭션에서 살아 있는지·쿼터 여유를 다시 확인하고
  기록한다. 삭제도 원 자산 행을 잠근 뒤 파생본을 찾으므로 클립은 삭제 증명에 함께 오르거나 만들어지지 않는다.
- 다시 해도 같은 실패(원 영상이 격리·삭제됨, 인코딩 실패, 쿼터 초과)는 재시도하지 않고 바로 FAILED (PermanentJobError).
  기록하지 못한 클립 객체는 지운다.
- 잠금 순서: 원 영상 행 → (무결성 로그) → 기관 쿼터 행 → 감사 로그 (삭제·다른 파생 작업과 같다).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.container import Container
from app.db.models import Asset, AssetLineage
from app.errors import ProblemError
from app.ports.queue import PermanentJobError
from app.services.ingest import Quota
from app.services.quality import apply_rules, load_rules
from app.timefmt import iso

log = logging.getLogger(__name__)
JOB_TYPE = "clip_export"
SOURCE_VARIANTS = ("ORIGINAL", "CLIP")
SOURCE_STATUSES = ("REGISTERED", "PROXIED", "READY_PRIVATE")
ACTIVE = ("QUEUED", "RUNNING")
RETRY_AFTER_S = 30  # 기관 한도에 닿았을 때 Retry-After (대기 작업이 언제 끝날지는 알 수 없다)


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


def request_clip(c: Container, db: Session, a: Asset, requested_by: str, start_s: float,
                 end_s: float) -> tuple[dict[str, Any], bool]:
    """작업을 등록한다. (응답, 새로 등록했는지). 같은 구간 작업이 대기·실행 중이면 그 작업을 돌려준다.

    작업 행은 db 트랜잭션 안에 넣는다 — 호출자가 감사와 함께 커밋한다. 원 영상 행을 먼저 잠가 같은 영상의 동시 요청과
    삭제를 직렬화하고, 기관 쿼터 행 잠금으로 기관의 대기 작업 수 세기와 등록을 직렬화한다."""
    db.refresh(a, with_for_update=True)  # 삭제·다른 요청이 끝난 뒤의 상태로 판단한다
    start, end = check_range(c, start_s, end_s, source_duration(a))
    for j in c.queue.jobs_for_asset(JOB_TYPE, a.asset_id):
        if j.status in ACTIVE and (_sec(j.payload["start_s"]), _sec(j.payload["end_s"])) == (start, end):
            return {"job_id": j.job_id, "asset_id": a.asset_id, "start_s": start, "end_s": end, "status": j.status,
                    "requested_at": iso(j.created_at)}, False
    # 클립 크기는 만들어 봐야 안다: 여유가 조금도 없으면 여기서 409, 작업이 저장 직전에 실제 크기로 다시 본다
    Quota(c, db).require_room(a.organization_id)
    conn = db.connection()
    limit = c.settings.clip_max_pending_per_org
    pending = c.queue.count_active(JOB_TYPE, a.organization_id, connection=conn)
    if pending >= limit:  # 한 기관이 긴 재인코딩 작업으로 공용 작업 큐를 채우지 않게
        raise ProblemError("RATE_LIMITED", detail=f"대기·실행 중인 클립 작업이 기관 한도({limit}건)에 닿았습니다. "
                           "작업이 끝난 뒤 다시 요청하세요", headers={"Retry-After": str(RETRY_AFTER_S)},
                           extra={"pending_jobs": pending, "limit": limit})
    now = c.clock.now()
    job_id = c.queue.enqueue(JOB_TYPE, {"asset_id": a.asset_id, "start_s": start, "end_s": end,
                                        "requested_by": requested_by}, organization_id=a.organization_id,
                             connection=conn)
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
    from app.services.pipeline import _run, local_copy, parse_probe, work_dir

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
        return store_clip(c, job_id, asset_id, out, start, end, video_ext)


@contextmanager
def _quota_fails_job() -> Iterator[None]:
    """쿼터 초과는 다시 해도 같다 — 재시도 없이 FAILED(QUOTA_EXCEEDED). 기관이 정리·증설한 뒤 다시 요청한다."""
    try:
        yield
    except ProblemError as exc:
        if exc.code != "QUOTA_EXCEEDED":
            raise
        raise PermanentJobError("QUOTA_EXCEEDED") from None


def store_clip(c: Container, job_id: str, asset_id: str, path: str, start: float, end: float,
               video_ext: dict[str, Any]) -> str:
    """만든 클립을 원 영상의 파생 자산으로 저장한다.

    1) 먼저 확인: 원 영상이 살아 있는지, 같은 결과가 이미 있는지, 클립 크기만큼 쿼터 여유가 있는지
       (아니면 올리지 않는다)
    2) 객체를 올린다 — 원 영상 행을 잠근 채 오래 걸리는 전송을 하지 않는다
    3) 원 영상 행을 잠근 트랜잭션에서 다시 확인하고 기록한다 (쿼터는 행을 잠근 뒤 실제 크기로, record_derived)
    올린 뒤 기록하지 못하면(원 영상 삭제·쿼터 초과·DB 오류) 올린 객체를 지운다.
    """
    from app.services.pipeline import _sha256_file, new_derived, record_derived, same_result, tool_version

    s = c.settings
    sha, size = _sha256_file(path)
    tool = f"{tool_version(s.ffmpeg_path)} libx264 crf{int(s.clip_crf)}"
    now = c.clock.now()
    with c.sessionmaker() as db:  # 쓰지 않는다 (쿼터 행은 확인하는 동안만 잠기고 닫을 때 되돌린다)
        p = db.get(Asset, asset_id)
        if not _usable(p):
            raise PermanentJobError("SOURCE_UNAVAILABLE")
        assert p is not None
        if same_result(db, p, sha) is not None:  # 같은 구간을 이미 만들었다 (같은 체크섬) — 새로 저장하지 않는다
            return "OK"
        with _quota_fails_job():
            Quota(c, db).require_room(p.organization_id, size)
        overrides: dict[str, Any] = {"ext": {"video_ext": video_ext} if video_ext else {}}
        if p.capture_start_at is not None:  # 촬영 시각은 원 영상 시작 + 구간 (미상이면 미상 그대로)
            overrides["capture_start_at"] = p.capture_start_at + timedelta(seconds=start)
            overrides["capture_end_at"] = p.capture_start_at + timedelta(seconds=end)
        child = new_derived(c, db, p, sha=sha, size=size, variant="CLIP", mime="video/mp4", relation="CLIP",
                            tool=tool, job_id=job_id, now=now, provenance_extra={"clip_range_s": [start, end]},
                            overrides=overrides)
        # 클립은 촬영 시각·구간을 가진 새 자산이다 — 등록 때와 같은 품질 규칙을 적용한다
        # (원 영상 촬영 시각이 미상이면 클립도 미상 → capture_time_unknown, ADR-0063)
        apply_rules(child, load_rules(db), now)
    assert child.storage_zone is not None and child.storage_key is not None  # new_derived가 정한다
    bucket, key = s.buckets[child.storage_zone], child.storage_key
    stored = False
    try:
        c.storage.upload_file(bucket, key, path, "video/mp4")
        with c.sessionmaker.begin() as db:
            # 삭제는 원 자산 행을 잠근 뒤 파생본을 찾는다 (retention.with_descendants). 여기서 먼저 잠그면
            # 삭제가 기다렸다가 이 클립까지 지우고(삭제 증명에 오름), 삭제가 먼저면 클립을 남기지 않는다
            p = db.get(Asset, asset_id, with_for_update=True, populate_existing=True)
            if not _usable(p):
                raise PermanentJobError("SOURCE_UNAVAILABLE")
            assert p is not None
            if same_result(db, p, sha) is not None:  # 같은 구간의 다른 작업이 그사이 저장했다 — 올린 사본은 지운다
                return "OK"
            with _quota_fails_job():
                record_derived(c, db, p, child, relation="CLIP", tool=tool, now=now, enforce_quota=True)
        stored = True
    finally:
        if not stored:
            try:
                c.storage.delete_object(bucket, key)
            except Exception as exc:  # noqa: BLE001 - 지우지 못한 객체는 사용량 대조(reconcile)에서 드러난다
                log.warning("clip_object_cleanup_failed", extra={"job_id": job_id, "error_type": type(exc).__name__})
    return "OK"
