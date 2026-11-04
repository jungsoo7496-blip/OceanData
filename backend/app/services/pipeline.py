"""처리 파이프라인 (05번): 메타 추출 → 프록시 → 썸네일 → READY_PRIVATE.

FFmpeg·ffprobe는 별도 프로세스로만 호출한다 (tech.md 2장: 라이브러리로 링크하지 않는다).
하드웨어 가속은 GPU_ENABLED와 PROXY_HW_ENCODER 설정으로만 켠다 (HW-2).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess  # nosec B404 - 고정된 ffmpeg·ffprobe 실행 파일만 인자 목록으로 호출한다
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.container import Container
from app.db.models import Asset, AssetLineage, Upload
from app.domain import states
from app.domain.ids import new_id
from app.domain.normalize import check_location
from app.domain.quality import evaluate
from app.metrics import PIPELINE_FRAMES, PIPELINE_SECONDS
from app.services.ingest import Quota, append_integrity, object_key, zone_for

log = logging.getLogger(__name__)
VIDEO_STEPS = ("media_probe", "make_proxy", "make_thumbnail")
STEP_FOR_JOB = {"media_probe": "META", "make_proxy": "PROXY", "make_thumbnail": "THUMBNAIL"}
JOB_FOR_STEP = {v: k for k, v in STEP_FOR_JOB.items()}


class CorruptMedia(Exception):
    """읽거나 재생할 수 없는 파일 → QUARANTINED(CORRUPT_FILE) (요구사항 3.3)."""


def _run(args: list[str], timeout_s: int) -> subprocess.CompletedProcess[bytes]:
    r = subprocess.run(args, capture_output=True, timeout=timeout_s, check=False)  # noqa: S603  # nosec B603
    if r.returncode != 0:  # 경로·URL은 scrub이 지운다. 원인 파악용으로 stderr 끝부분만 남긴다
        log.warning("ffmpeg_failed", extra={"tool": os.path.basename(args[0]), "returncode": r.returncode,
                                            "stderr_tail": r.stderr.decode(errors="replace")[-500:]})
    return r


def tool_version(binary: str) -> str:
    try:
        out = _run([binary, "-version"], 10).stdout.decode(errors="replace").splitlines()
        return out[0][:120] if out else binary
    except (OSError, subprocess.TimeoutExpired):
        return binary


@contextmanager
def local_copy(c: Container, asset: Asset) -> Iterator[str]:
    """객체를 임시 파일로 스트리밍해 받아 둔다 (메모리에 올리지 않는다)."""
    bucket = c.settings.buckets[asset.storage_zone or zone_for(asset.variant)]
    fd, path = tempfile.mkstemp(prefix="marine-", suffix="." + (asset.mime_type.split("/")[-1] or "bin"))
    try:
        with os.fdopen(fd, "wb") as f:
            for chunk in c.storage.iter_object(bucket, asset.storage_key or ""):
                f.write(chunk)
        yield path
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------- 메타 추출

_ISO6709 = re.compile(r"^([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)")


def parse_probe(probe: dict[str, Any]) -> dict[str, Any]:
    """ffprobe JSON → video_ext·촬영시각 근거·GPS (순수 함수, 시험 가능)."""
    fmt = probe.get("format") or {}
    video = next((s for s in probe.get("streams") or [] if s.get("codec_type") == "video"), None)
    if video is None:
        raise CorruptMedia("영상 스트림이 없습니다")
    ext: dict[str, Any] = {}
    if video.get("width") and video.get("height"):
        ext["resolution"] = f"{int(video['width'])}x{int(video['height'])}"
    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/0"
    try:
        num, den = (float(x) for x in rate.split("/"))
        if den > 0 and num > 0:
            ext["fps"] = round(num / den, 3)
    except ValueError:
        pass
    if video.get("codec_name"):
        ext["codec"] = str(video["codec_name"])
    if fmt.get("format_name"):
        ext["container"] = str(fmt["format_name"]).split(",")[0]
    if fmt.get("bit_rate"):
        ext["bitrate_mbps"] = round(int(fmt["bit_rate"]) / 1_000_000, 3)
    if fmt.get("duration"):
        ext["duration_s"] = round(float(fmt["duration"]), 3)
    rotate = (video.get("tags") or {}).get("rotate")
    if rotate is not None:
        ext["orientation_deg"] = float(rotate) % 360
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    created = None
    raw_created = tags.get("creation_time")
    if raw_created:
        try:
            dt = datetime.fromisoformat(str(raw_created).replace("Z", "+00:00"))
            if dt.tzinfo is not None and dt.year >= 2000:
                created = dt.astimezone(UTC)
        except ValueError:
            created = None
    location = None
    iso = tags.get("location") or tags.get("com.apple.quicktime.location.iso6709")
    if iso:
        m = _ISO6709.match(str(iso))
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
            geom = {"type": "Point", "coordinates": [lon, lat]}
            if not check_location(geom):
                location = geom
    return {"video_ext": ext, "creation_time": created, "creation_time_raw": raw_created, "location": location}


def media_probe(c: Container, asset_id: str) -> str:
    with c.sessionmaker() as db:
        asset = db.get(Asset, asset_id)
        if asset is None or not asset.has_binary or asset.ingest_status != "REGISTERED":
            return "SKIPPED"
        snapshot = Asset(**{k: getattr(asset, k) for k in ("storage_zone", "storage_key", "variant", "mime_type")})
    with local_copy(c, snapshot) as path:
        r = _run([c.settings.ffprobe_path, "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                  path], c.settings.ffmpeg_timeout_s)
        try:
            if r.returncode != 0:
                raise CorruptMedia("ffprobe 실패")
            meta = parse_probe(json.loads(r.stdout or b"{}"))
        except (CorruptMedia, json.JSONDecodeError):
            return quarantine_corrupt(c, asset_id)
    with c.sessionmaker.begin() as db:
        asset = db.execute(select(Asset).where(Asset.asset_id == asset_id).with_for_update()).scalar_one()
        asset.ext = {**(asset.ext or {}), "video_ext": {**(asset.ext or {}).get("video_ext", {}), **meta["video_ext"]}}
        ts = asset.timestamp_quality or {}
        if ts.get("source", "UNKNOWN") == "UNKNOWN" and meta["creation_time"] is not None:
            # 컨테이너 내부 생성 시각 태그 → DEVICE, verified=false (요구사항 3.2)
            asset.capture_start_at = meta["creation_time"]
            if meta["video_ext"].get("duration_s"):
                asset.capture_end_at = meta["creation_time"] + timedelta(seconds=meta["video_ext"]["duration_s"])
            asset.timestamp_quality = {"source": "DEVICE", "verified": False, "est_offset_ms": None}
            originals = {**asset.provenance.get("original_timestamps", {}),
                         "container_creation_time": meta["creation_time_raw"]}
            asset.provenance = {**asset.provenance, "original_timestamps": originals}
        if asset.location is None and meta["location"] is not None:
            asset.location, asset.location_precision = meta["location"], "EXACT_RESTRICTED"
        asset.quality_status, asset.quality_reasons = evaluate(_fields(asset), facts={"playable": True})
        asset.updated_at = c.clock.now()
    return "OK"


def _fields(a: Asset) -> dict[str, Any]:
    return {"data_type": a.data_type, "mime_type": a.mime_type, "byte_size": a.byte_size,
            "checksum_sha256": a.checksum_sha256, "timestamp_quality": a.timestamp_quality}


def quarantine_corrupt(c: Container, asset_id: str) -> str:
    with c.sessionmaker.begin() as db:
        asset = db.execute(select(Asset).where(Asset.asset_id == asset_id).with_for_update()).scalar_one()
        asset.ingest_status = states.transition("ingest_status", asset.ingest_status, "QUARANTINED", {"SERVICE"})
        asset.quarantine_reason = "CORRUPT_FILE"
        asset.quality_status, asset.quality_reasons = "FAILED", ["not_playable"]
        asset.retention_until = c.clock.now() + timedelta(days=c.settings.quarantine_retention_days)
        asset.updated_at = c.clock.now()
        bucket, key = c.settings.buckets[asset.storage_zone or "RAW"], asset.storage_key or ""
        size = asset.byte_size
        org = asset.organization_id
        if asset.storage_zone != "QUARANTINE":
            try:
                c.storage.move_object(bucket, key, c.settings.buckets["QUARANTINE"], key)
                asset.storage_zone = "QUARANTINE"
                q = Quota(c, db)._row(org)
                q.used_bytes, q.quarantine_bytes = max(0, q.used_bytes - size), q.quarantine_bytes + size
            except Exception as exc:  # noqa: BLE001
                log.error("quarantine_move_failed", extra={"asset_id": asset_id, "error_type": type(exc).__name__})
    log.warning("asset_corrupt", extra={"asset_id": asset_id})
    return "QUARANTINED"


# ---------------------------------------------------------------- 파생 자산

def _sha256_file(path: str) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def create_derived(c: Container, db: Session, parent: Asset, path: str, *, variant: str, mime: str,
                   relation: str, tool: str, job_id: str | None) -> Asset:
    """원 자산에서 만든 파생 자산을 저장하고 계보·무결성 로그를 남긴다 (요구사항 4.4, 5.1).

    파생본은 원 자산의 data_role·sensitivity_class를 물려받고 PRIVATE으로 시작한다 (domain.md 3·4장).
    """
    sha, size = _sha256_file(path)
    now = c.clock.now()
    existing = db.execute(select(Asset).where(Asset.organization_id == parent.organization_id,
                                              Asset.source_system_id == parent.source_system_id,
                                              Asset.checksum_sha256 == sha)).scalar_one_or_none()
    if existing is not None:  # 같은 결과가 이미 있으면 계보만 확인한다 (재실행 멱등)
        return existing
    child = Asset(
        asset_id=new_id("asset"), schema_version=parent.schema_version, organization_id=parent.organization_id,
        project_id=parent.project_id, session_id=parent.session_id, source_system_id=parent.source_system_id,
        data_type="IMAGE" if variant == "THUMBNAIL" else parent.data_type, variant=variant,
        data_role=parent.data_role, capture_start_at=parent.capture_start_at, capture_end_at=parent.capture_end_at,
        ingested_at=now, timestamp_quality=parent.timestamp_quality, location=parent.location,
        location_precision=parent.location_precision, area_code=parent.area_code, mime_type=mime, byte_size=size,
        checksum_sha256=sha, has_binary=True, collection_mode=parent.collection_mode,
        privacy_status=parent.privacy_status, sensitivity_class=parent.sensitivity_class, sharing_scope="PRIVATE",
        retention_policy_id=parent.retention_policy_id, retention_until=parent.retention_until,
        quality_status="PASSED", quality_reasons=[], share_status="NONE", backup_class="NONE",
        # 확인 필요: domain.md 5장 표에 파이프라인이 파생 자산을 만드는 행((없음) → READY_PRIVATE, SERVICE)이 없다
        ingest_status="READY_PRIVATE",
        provenance={"ingest_channel": "PIPELINE", "parent_asset_ids": [parent.asset_id], "transform": relation,
                    "tool_version": tool, **({"job_id": job_id} if job_id else {})},
        ext={}, created_at=now, updated_at=now)
    child.storage_zone = zone_for(variant)
    child.storage_key = object_key(child)
    c.storage.upload_file(c.settings.buckets[child.storage_zone], child.storage_key, path, mime)
    db.add(child)
    db.flush()
    db.add(AssetLineage(child_asset_id=child.asset_id, parent_asset_id=parent.asset_id, relation=relation,
                        tool_version=tool, run_by="SERVICE", created_at=now))
    append_integrity(db, child, c.clock.now())
    q = Quota(c, db)._row(parent.organization_id)
    q.used_bytes += size  # PROCESSED 존도 기관 사용량에 들어간다 (domain.md 9장)
    return child


def _short_side_scale(short: int) -> str:
    # 짧은 변을 short 픽셀로, 긴 변은 비율 유지(짝수). 원본이 더 작으면 키우지 않는다
    return (f"scale='if(gt(iw,ih),-2,min({short},iw))':'if(gt(iw,ih),min({short},ih),-2)'")


def proxy_args(s: Any, src: str, out: str, encoder: str) -> list[str]:
    args = [s.ffmpeg_path, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
            "-vf", _short_side_scale(s.proxy_short_side), "-c:v", encoder]
    if encoder == "libx264":
        args += ["-preset", "veryfast"]
    return args + ["-b:v", s.proxy_max_bitrate, "-maxrate", s.proxy_max_bitrate, "-bufsize", s.proxy_max_bitrate,
                   "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-c:a", "aac", "-b:a", "96k",
                   "-map_metadata", "-1", out]


def thumbnail_args(s: Any, src: str, out: str, duration_s: float) -> list[str]:
    interval = max(1.0, float(s.thumbnail_interval_s))
    frames = max(1, min(s.thumbnail_max_frames, int(duration_s // interval) + 1 if duration_s else 1))
    cols = min(5, frames)
    rows = (frames + cols - 1) // cols
    # 첫 프레임부터 간격마다 고른다 (fps 필터는 간격보다 짧은 영상에서 프레임을 내지 않을 수 있다)
    return [s.ffmpeg_path, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
            "-vf", f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{interval})',scale=320:-2,"
                   f"tile={cols}x{rows}", "-fps_mode", "vfr", "-frames:v", "1", "-q:v", "4", out]


def make_proxy(c: Container, asset_id: str, job_id: str | None = None) -> str:
    s = c.settings
    with c.sessionmaker() as db:
        parent = db.get(Asset, asset_id)
        if parent is None or parent.ingest_status not in ("REGISTERED", "PROXIED", "READY_PRIVATE"):
            return "SKIPPED"
        db.expunge(parent)
    encoder = s.proxy_hw_encoder if (s.gpu_enabled and s.proxy_hw_encoder) else "libx264"
    with local_copy(c, parent) as src, tempfile.TemporaryDirectory(prefix="marine-proxy-") as tmp:
        out = os.path.join(tmp, "proxy.mp4")
        args = proxy_args(s, src, out, encoder)
        started = time.perf_counter()
        r = _run(args, s.ffmpeg_timeout_s)
        if r.returncode != 0 and encoder != "libx264":
            log.warning("hw_encoder_failed_fallback_cpu", extra={"encoder": encoder})  # 하드웨어가 없으면 CPU로
            args = proxy_args(s, src, out, "libx264")
            r = _run(args, s.ffmpeg_timeout_s)
        if r.returncode != 0:
            return quarantine_corrupt(c, asset_id) if parent.ingest_status == "REGISTERED" else "FAILED"
        ve = (parent.ext or {}).get("video_ext") or {}
        if ve.get("fps") and ve.get("duration_s"):  # 초당 처리 프레임 (요구사항 9.1)
            PIPELINE_FRAMES.labels("make_proxy").inc(float(ve["fps"]) * float(ve["duration_s"]))
            PIPELINE_SECONDS.labels("make_proxy").inc(time.perf_counter() - started)
        with c.sessionmaker.begin() as db:
            p = db.get(Asset, asset_id)
            assert p is not None
            create_derived(c, db, p, out, variant="PROXY", mime="video/mp4", relation="PROXY",
                           tool=f"{tool_version(s.ffmpeg_path)} {encoder}", job_id=job_id)
    return "OK"


def make_thumbnail(c: Container, asset_id: str, job_id: str | None = None) -> str:
    """설정 간격의 대표 프레임을 한 장의 접촉 인쇄(contact sheet) JPEG로 만든다 (요구사항 4.3)."""
    s = c.settings
    with c.sessionmaker() as db:
        parent = db.get(Asset, asset_id)
        if parent is None or parent.ingest_status not in ("REGISTERED", "PROXIED", "READY_PRIVATE"):
            return "SKIPPED"
        db.expunge(parent)
    duration = float(((parent.ext or {}).get("video_ext") or {}).get("duration_s") or 0)
    with local_copy(c, parent) as src, tempfile.TemporaryDirectory(prefix="marine-thumb-") as tmp:
        out = os.path.join(tmp, "thumb.jpg")
        r = _run(thumbnail_args(s, src, out, duration), s.ffmpeg_timeout_s)
        if r.returncode != 0 or not os.path.exists(out):
            return "FAILED"
        with c.sessionmaker.begin() as db:
            p = db.get(Asset, asset_id)
            assert p is not None
            create_derived(c, db, p, out, variant="THUMBNAIL", mime="image/jpeg", relation="THUMBNAIL",
                           tool=tool_version(s.ffmpeg_path), job_id=job_id)
            # 프록시·썸네일 완료 → PROXIED → READY_PRIVATE (요구사항 2.2)
            if p.ingest_status == "REGISTERED":
                p.ingest_status = states.transition("ingest_status", "REGISTERED", "PROXIED", {"SERVICE"})
            if p.ingest_status == "PROXIED":
                p.ingest_status = states.transition("ingest_status", "PROXIED", "READY_PRIVATE", {"SERVICE"})
            p.updated_at = c.clock.now()
    return "OK"


def schedule_after_register(c: Container, asset: Asset) -> None:
    """REGISTERED 후 작업 등록 (요구사항 2.1, 2.3, 2.5). 다른 스펙은 STEP_HANDLERS에 유형을 더한다."""
    if asset.data_type == "VIDEO" and asset.has_binary and asset.variant in ("ORIGINAL", "CLIP"):
        c.queue.enqueue("media_probe", {"asset_id": asset.asset_id, "chain": list(VIDEO_STEPS[1:])},
                        organization_id=asset.organization_id, dedupe_key=f"media_probe:{asset.asset_id}")


def next_step(c: Container, payload: dict[str, Any], org: str | None) -> None:
    """메타 추출 → 프록시 → 썸네일을 순서대로 이어 등록한다."""
    chain = list(payload.get("chain") or [])
    if chain:
        nxt = chain.pop(0)
        c.queue.enqueue(nxt, {"asset_id": payload["asset_id"], "chain": chain, "run": payload.get("run", "")},
                        organization_id=org, dedupe_key=f"{nxt}:{payload['asset_id']}:{payload.get('run', '')}")


def mark_failed(c: Container, asset_id: str, error_code: str) -> None:
    """재시도를 다 쓴 처리 단계 → FAILED (domain.md 5장 '처리 단계 → FAILED')."""
    with c.sessionmaker.begin() as db:
        a = db.execute(select(Asset).where(Asset.asset_id == asset_id).with_for_update()).scalar_one_or_none()
        if a is not None and states.can_transition("ingest_status", a.ingest_status, "FAILED", {"SERVICE"}):
            a.ingest_context = {**(a.ingest_context or {}), "failed_from": a.ingest_status, "error_code": error_code}
            a.ingest_status = "FAILED"
            a.updated_at = c.clock.now()


def find_upload(db: Session, asset_id: str) -> Upload | None:
    return db.execute(select(Upload).where(Upload.asset_id == asset_id).order_by(Upload.created_at.desc())
                      .limit(1)).scalar_one_or_none()


def ffmpeg_available(c: Container) -> bool:
    return bool(shutil.which(c.settings.ffmpeg_path)) and bool(shutil.which(c.settings.ffprobe_path))
