"""선별 비식별·표본 검수 (14번 요구사항 1~5, 8, 9).

- 대상 제한: share_status=PROCESSING인 원본만 (공유 심사에서 법률 검토를 통과한 것). 일괄 처리 기능은 없다.
- 탐지(DetectorPort) → 추적 보간 → ffmpeg로 가린 새 DEID 파생본(PROCESSED 존). 원본은 그대로.
- 끝나면 원본 PROCESSING → QUALITY_REVIEW, 검수자 배정. 통과하면 파생본에만 PASSED·PSEUDONYMIZED.
- 화면·산출물 표기는 "처리 이력"이다. 자동 처리만으로 적정성 보증·면책을 표시하지 않는다 (요구사항 5.4).
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
import time
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.detectors import build_detector
from app.db.models import Asset, DeidReview, ReviewSamplingPolicy, User
from app.domain import deid as rules
from app.domain import states
from app.domain.ids import new_id
from app.metrics import PIPELINE_FRAMES, PIPELINE_SECONDS
from app.ports.notifier import Notification
from app.services import sharing
from app.timefmt import iso

if TYPE_CHECKING:
    from app.container import Container

log = logging.getLogger(__name__)
PROCESS_NOTE = "처리 이력: 자동 탐지·가림 결과이며 적정성을 보증하지 않습니다. 검수 결과와 함께 판단하세요."


def _size(a: Asset) -> tuple[int, int]:
    res = ((a.ext or {}).get("video_ext") or {}).get("resolution") or "0x0"
    w, _, h = str(res).partition("x")
    return int(w or 0), int(h or 0)


def deidentify(c: Container, asset_id: str, job_id: str | None, payload: dict[str, Any]) -> str:
    """Deid_Job (요구사항 1~3). payload: classes, method, tracking, manual_regions(검수자 추가 영역)."""
    from app.services.pipeline import _run, create_derived, local_copy, tool_version, work_dir

    s = c.settings
    with c.sessionmaker() as db:
        a = db.get(Asset, asset_id)
        if a is None or a.variant != "ORIGINAL" or a.data_type != "VIDEO" or a.share_status != "PROCESSING":
            log.warning("deid_skipped", extra={"asset_id": asset_id, "share_status": getattr(a, "share_status", None)})
            return "SKIPPED"  # 요구사항 1.1: 공유 심사 중인 원본만
        db.expunge(a)
    classes = [x for x in payload.get("classes", s.deid_classes) if x in rules.CLASSES]
    method = payload.get("method", s.deid_method)
    width, height = _size(a)
    detector = build_detector(s)
    started = time.perf_counter()
    with local_copy(c, a) as src, tempfile.TemporaryDirectory(prefix="marine-deid-", dir=work_dir(c)) as tmp:
        found = detector.detect(a, src, classes, s.deid_sample_fps)
        regions = rules.track(found, s.deid_track_gap_s) if payload.get("tracking", s.deid_tracking) else found
        manual = [rules.Region("MANUAL", float(r["t0"]), float(r["t1"]), int(r["x"]), int(r["y"]), int(r["w"]),
                               int(r["h"])) for r in payload.get("manual_regions", [])]
        out = os.path.join(tmp, "deid.mp4")
        graph = rules.filter_graph([*regions, *manual], method, width or 10_000, height or 10_000)
        args = [s.ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-filter_complex", graph,
                "-map", "[out]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p", "-c:a", "copy", out]
        r = _run(args, s.ffmpeg_timeout_s)
        if r.returncode != 0:
            return "FAILED"
        ve = (a.ext or {}).get("video_ext") or {}
        if ve.get("fps") and ve.get("duration_s"):  # 초당 처리 프레임 (요구사항 8.2)
            PIPELINE_FRAMES.labels("deidentify").inc(float(ve["fps"]) * float(ve["duration_s"]))
            PIPELINE_SECONDS.labels("deidentify").inc(time.perf_counter() - started)
        with c.sessionmaker.begin() as db:
            orig = db.get(Asset, asset_id, with_for_update=True)
            assert orig is not None  # noqa: S101
            m = detector.model
            child = create_derived(c, db, orig, out, variant="DEID", mime="video/mp4", relation="DEID",
                                   tool=f"{tool_version(s.ffmpeg_path)} {m.name}@{m.version}", job_id=job_id)
            record = {"model": {"name": m.name, "version": m.version, "sha256": m.sha256, "license": m.license},
                      "params": {"classes": classes, "method": method, "tracking": bool(payload.get(
                          "tracking", s.deid_tracking)), "sample_fps": s.deid_sample_fps},
                      "detections": rules.counts_by_class(regions), "manual_regions": len(manual),
                      "regions": [r.__dict__ for r in [*regions, *manual]][:500], "processed_at": iso(c.clock.now()),
                      "note": PROCESS_NOTE}
            child.ext = {**(child.ext or {}), "deid": record}
            child.provenance = {**(child.provenance or {}), "deid": {k: record[k] for k in
                                                                     ("model", "params", "detections", "processed_at")}}
            child.privacy_status, child.review_result = "REVIEW_REQUIRED", None
            for old in [d for d in sharing.descendants(db, orig.asset_id)
                        if d.variant == "DEID" and d.asset_id != child.asset_id and d.review_result != "FAILED"]:
                old.review_result = "FAILED"  # 다시 만든 결과로 대체된 이전 비식별본은 공유 대상에서 빠진다
            if orig.share_status == "PROCESSING":
                sharing.mark_processed(c, db, orig)  # 요구사항 5.1
            assign(c, db, orig, child, recheck=False)
    return "OK"


def risk_level(child: Asset) -> str:
    """위험도 초안: 얼굴·번호판 탐지 또는 RESTRICTED 이상 → HIGH, 다른 탐지 → MEDIUM, 없음 → LOW."""
    det = ((child.ext or {}).get("deid") or {}).get("detections") or {}
    if det.get("FACE") or det.get("LICENSE_PLATE") or child.sensitivity_class != "INTERNAL":
        return "HIGH"
    return "MEDIUM" if any(det.values()) else "LOW"


def sample_times(duration_s: float, ratio: float, min_samples: int) -> list[float]:
    """1초 구간 중 ratio만큼(최소 min_samples) 고르게 표본 시각."""
    if duration_s <= 0:
        return [0.0]
    n = max(min_samples, math.ceil(duration_s * ratio))
    n = min(n, max(1, int(duration_s * 4)))
    return [round((i + 0.5) * duration_s / n, 3) for i in range(n)]


def _pick_reviewer(db: Session) -> User | None:
    open_counts: dict[str, int] = {r: int(n) for r, n in db.execute(
        select(DeidReview.reviewer_id, func.count()).where(DeidReview.status == "ASSIGNED")
        .group_by(DeidReview.reviewer_id)).all()}
    is_reviewer = User.roles.any("REVIEWER")  # type: ignore[arg-type]
    reviewers = list(db.execute(select(User).where(User.status == "ACTIVE", is_reviewer)
                                .order_by(User.user_id)).scalars())
    return min(reviewers, key=lambda u: (open_counts.get(u.user_id, 0), u.user_id)) if reviewers else None


def assign(c: Container, db: Session, orig: Asset, child: Asset, recheck: bool) -> DeidReview | None:
    reviewer = _pick_reviewer(db)
    if reviewer is None:
        log.error("no_reviewer", extra={"asset_id": child.asset_id})
        return None
    level = risk_level(child)
    pol = db.get(ReviewSamplingPolicy, level)
    duration = float(((child.ext or {}).get("video_ext") or (orig.ext or {}).get("video_ext") or {})
                     .get("duration_s") or 0)
    rv = DeidReview(review_id=new_id("review"), organization_id=orig.organization_id, session_id=orig.session_id,
                    original_asset_id=orig.asset_id, deid_asset_id=child.asset_id, reviewer_id=reviewer.user_id,
                    risk_level=level, samples=sample_times(duration, pol.sample_ratio if pol else 1.0,
                                                           pol.min_samples if pol else 5),
                    recheck=recheck, status="ASSIGNED", missed=[], manual_regions=[], created_at=c.clock.now())
    db.add(rv)
    if reviewer.contact_email:
        c.notifier.send(Notification(kind="deid.review_assigned", organization_id=None,
                                     subject=f"[검수 배정] {'재검수 ' if recheck else ''}{child.asset_id}",
                                     body=f"비식별 결과 검수 배정: 표본 {len(rv.samples)}개, 위험도 {level}.",
                                     recipients=(reviewer.contact_email,), data={"review_id": rv.review_id}))
    return rv


def decide(c: Container, db: Session, rv: DeidReview, result: str, missed: list[dict[str, Any]],
           manual: list[dict[str, Any]], note: str | None, actor_roles: set[str], actor: str) -> list[str]:
    """검수 결정 (요구사항 4.2~4.4, 5.2). 다시 검수 대상으로 표시한 리뷰 ID 목록을 돌려준다."""
    if rv.status != "ASSIGNED":
        raise states.InvalidStateTransition("deid_review", rv.status, result)
    now = c.clock.now()
    child = db.get(Asset, rv.deid_asset_id, with_for_update=True)
    orig = db.get(Asset, rv.original_asset_id, with_for_update=True)
    assert child is not None and orig is not None  # noqa: S101
    rv.missed, rv.manual_regions, rv.note, rv.decided_at = missed, manual, note, now
    flagged: list[str] = []
    if result == "PASS":
        rv.status = "PASSED"
        child.review_result, child.privacy_status = "PASSED", "PSEUDONYMIZED"  # 파생본에만 (원본 아님)
    elif result == "MISSED":
        rv.status = "MISSED"
        child.review_result = "FAILED"
        if manual:  # 수동 가림 영역을 더해 다시 렌더링 (요구사항 4.3)
            _rerender(c, db, orig, manual, actor_roles)
        flagged = _recheck_session(c, db, rv)  # 같은 회차 재검수 (요구사항 4.4, T09-b)
    else:  # REJECT → 재처리
        rv.status = "REJECTED"
        child.review_result = "FAILED"
        _rerender(c, db, orig, [], actor_roles)
    child.updated_at = now
    return flagged


def _rerender(c: Container, db: Session, orig: Asset, manual: list[dict[str, Any]], roles: set[str]) -> None:
    """QUALITY_REVIEW → REPROCESS(검수자·운영자) → PROCESSING(SERVICE, 재처리 등록)."""
    now = c.clock.now()
    if orig.share_status == "QUALITY_REVIEW":
        sharing.review(c, db, orig, "REQUEST_REPROCESS", roles | {"REVIEWER"}, "deid-review", "검수 결과 재처리",
                       None, None)
    if orig.share_status == "REPROCESS":
        sharing._review_event(db, orig, "PROCESSING", "SERVICE", "재처리 등록", None, now)
        orig.share_status = states.transition("share_status", "REPROCESS", "PROCESSING", {"SERVICE"})
    c.queue.enqueue("deidentify", {"asset_id": orig.asset_id, "manual_regions": manual},
                    organization_id=orig.organization_id,
                    dedupe_key=f"deidentify:{orig.asset_id}:{int(now.timestamp() * 1000)}")


def _recheck_session(c: Container, db: Session, rv: DeidReview) -> list[str]:
    if not rv.session_id:
        return []
    out = []
    others = db.execute(select(DeidReview).where(
        DeidReview.session_id == rv.session_id, DeidReview.review_id != rv.review_id,
        DeidReview.status == "PASSED")).scalars()
    seen: set[str] = set()
    for o in others:
        if o.deid_asset_id in seen:
            continue
        seen.add(o.deid_asset_id)
        child = db.get(Asset, o.deid_asset_id)
        orig = db.get(Asset, o.original_asset_id)
        if child is None or orig is None:
            continue
        child.review_result = None  # 재검수 전까지 공유 조건(review_result=PASSED)에서 빠진다
        new = assign(c, db, orig, child, recheck=True)
        if new is not None:
            out.append(new.review_id)
    return out


def quality_report(db: Session, organization_id: str | None = None) -> dict[str, Any]:
    """검수 결과로 클래스별 미탐률·재처리 비율 (요구사항 9.1, T09-c)."""
    q = select(DeidReview).where(DeidReview.status.in_(["PASSED", "MISSED", "REJECTED"]))
    if organization_id:
        q = q.where(DeidReview.organization_id == organization_id)
    reviews = list(db.execute(q).scalars())
    detected: dict[str, int] = {}
    missed: dict[str, int] = {}
    for rv in reviews:
        child = db.get(Asset, rv.deid_asset_id)
        for k, n in (((child.ext or {}).get("deid") or {}).get("detections") or {}).items() if child else []:
            detected[k] = detected.get(k, 0) + int(n)
        for m in rv.missed:
            missed[m.get("cls", "UNKNOWN")] = missed.get(m.get("cls", "UNKNOWN"), 0) + 1
    classes = sorted(set(detected) | set(missed))
    per_class = [{"class": k, "detected": detected.get(k, 0), "missed": missed.get(k, 0),
                  "miss_rate": round(missed.get(k, 0) / (detected.get(k, 0) + missed.get(k, 0)), 4)
                  if detected.get(k, 0) + missed.get(k, 0) else 0.0} for k in classes]
    redo = sum(1 for r in reviews if r.status in ("MISSED", "REJECTED"))
    return {"reviews": len(reviews), "reprocess_ratio": round(redo / len(reviews), 4) if reviews else 0.0,
            "classes": per_class, "note": PROCESS_NOTE}


def review_view(rv: DeidReview, child: Asset | None) -> dict[str, Any]:
    d = ((child.ext or {}).get("deid") or {}) if child else {}
    return {"review_id": rv.review_id, "organization_id": rv.organization_id, "session_id": rv.session_id,
            "original_asset_id": rv.original_asset_id, "deid_asset_id": rv.deid_asset_id,
            "reviewer_id": rv.reviewer_id, "risk_level": rv.risk_level, "samples": rv.samples, "recheck": rv.recheck,
            "status": rv.status, "missed": rv.missed, "manual_regions": rv.manual_regions, "note": rv.note,
            "decided_at": iso(rv.decided_at), "created_at": iso(rv.created_at),
            "processing": {k: d.get(k) for k in ("model", "params", "detections", "processed_at", "note")}}

