"""비식별 검수·품질 집계·태그·장면 검색 (14번)."""

from __future__ import annotations

import os
import tempfile
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import cast, column, literal, select, table
from sqlalchemy.orm import Session

from app.api.deps import client_ip, container, get_db, load_scoped, require
from app.container import Container
from app.db.models import Asset, AssetTag, AssetTagEdit, DeidReview
from app.db.types import Vector
from app.domain.principal import Principal
from app.errors import ProblemError
from app.services import deid, tagging
from app.services.tenant import scope_filter
from app.timefmt import iso

router = APIRouter()
SE = table("scene_embeddings", column("asset_id"), column("t_offset"), column("model"), column("embedding"))


def _operator(p: Principal) -> bool:
    return not p.is_machine and p.has_role("PLATFORM_OPERATOR")


def _review(db: Session, p: Principal, review_id: str, lock: bool = False) -> DeidReview:
    rv = db.get(DeidReview, review_id, with_for_update=lock)
    if rv is None or (not _operator(p) and rv.reviewer_id != p.user_id):  # 배정된 대상만 (요구사항 4.5)
        raise ProblemError("NOT_FOUND")
    return rv


@router.get("/v1/deid-reviews", tags=["deid"])
def list_reviews(status: Literal["ASSIGNED", "PASSED", "MISSED", "REJECTED"] | None = None,
                 db: Session = Depends(get_db), p: Principal = Depends(require("deid_review:read"))) -> dict:
    stmt = select(DeidReview).order_by(DeidReview.created_at, DeidReview.review_id)
    if not _operator(p):
        stmt = stmt.where(DeidReview.reviewer_id == p.user_id)
    if status:
        stmt = stmt.where(DeidReview.status == status)
    return {"items": [deid.review_view(r, db.get(Asset, r.deid_asset_id)) for r in db.execute(stmt).scalars()]}


@router.get("/v1/deid-reviews/{review_id}", tags=["deid"])
def get_review(review_id: str, db: Session = Depends(get_db),
               p: Principal = Depends(require("deid_review:read"))) -> dict:
    rv = _review(db, p, review_id)
    return deid.review_view(rv, db.get(Asset, rv.deid_asset_id))


@router.get("/v1/deid-reviews/{review_id}/frame", tags=["deid"])
def review_frame(review_id: str, request: Request, t: float = Query(ge=0), which: Literal["ORIGINAL", "DEID"] = "DEID",
                 db: Session = Depends(get_db), c: Container = Depends(container),
                 p: Principal = Depends(require("deid_review:read"))) -> Response:
    """표본 프레임 (원본 대비, 요구사항 4.2). 원본 열람은 감사 로그에 남는다."""
    from app.services.pipeline import _run, local_copy, work_dir

    rv = _review(db, p, review_id)
    a = db.get(Asset, rv.original_asset_id if which == "ORIGINAL" else rv.deid_asset_id)
    if a is None or not a.storage_key:
        raise ProblemError("NOT_FOUND")
    db.expunge(a)
    with local_copy(c, a) as src, tempfile.TemporaryDirectory(prefix="marine-frame-", dir=work_dir(c)) as tmp:
        out = os.path.join(tmp, "f.jpg")
        r = _run([c.settings.ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", src,
                  "-frames:v", "1", "-q:v", "4", out], 120)
        if r.returncode != 0 or not os.path.exists(out):
            raise ProblemError("NOT_FOUND", detail="그 시각의 프레임을 만들 수 없습니다")
        data = open(out, "rb").read()  # noqa: SIM115 - 임시 디렉터리 안 작은 파일
    from app.services.tiers import touch_accessed

    touch_accessed(db, a.asset_id, c.clock.now())  # 검수자가 바이너리를 열었다 (계층 규칙의 '마지막 조회')
    c.audit.record("DEID_REVIEW_FRAME_VIEWED", principal=p, organization_id=rv.organization_id, target_type="Asset",
                   target_id=a.asset_id, source_ip=client_ip(request), details={"which": which, "t": t})
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


class Missed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cls: Literal["FACE", "LICENSE_PLATE", "VESSEL_ID_TEXT", "SCREEN_TEXT"]
    t: float = Field(ge=0)


class ManualRegion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    t0: float = Field(ge=0)
    t1: float = Field(ge=0)
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    w: int = Field(ge=2)
    h: int = Field(ge=2)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result: Literal["PASS", "MISSED", "REJECT"]
    missed: list[Missed] = Field(default_factory=list, max_length=500)
    manual_regions: list[ManualRegion] = Field(default_factory=list, max_length=500)
    note: str | None = Field(None, max_length=2000)


@router.post("/v1/deid-reviews/{review_id}/decision", tags=["deid"])
def decide_review(review_id: str, body: Decision, request: Request, db: Session = Depends(get_db),
                  c: Container = Depends(container), p: Principal = Depends(require("deid_review:decide"))) -> dict:
    rv = _review(db, p, review_id, lock=True)
    if body.result == "MISSED" and not body.missed:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/missed", "code": "required",
                                                        "message": "미탐을 표시하세요 (클래스·시각)"}])
    flagged = deid.decide(c, db, rv, body.result, [m.model_dump() for m in body.missed],
                          [m.model_dump() for m in body.manual_regions], body.note, set(p.roles), p.principal_id)
    c.audit.record("DEID_REVIEW_DECIDED", principal=p, organization_id=rv.organization_id, target_type="Asset",
                   target_id=rv.deid_asset_id, source_ip=client_ip(request),
                   details={"review_id": rv.review_id, "result": body.result, "missed": len(body.missed),
                            "recheck_reviews": flagged})
    db.flush()
    return {**deid.review_view(rv, db.get(Asset, rv.deid_asset_id)), "recheck_review_ids": flagged}


@router.get("/v1/deid-quality", tags=["deid"])
def quality(db: Session = Depends(get_db), p: Principal = Depends(require("deid_quality:read"))) -> dict:
    """클래스별 미탐률·재처리 비율 (요구사항 9.1). 제공기관 관리자는 자기 기관만."""
    return deid.quality_report(db, None if _operator(p) else p.organization_id)


# ------------------------------------------------------------------ 태그·장면 검색 (요구사항 6, 7)


@router.get("/v1/assets/{asset_id}/tags", tags=["deid"])
def get_tags(asset_id: str, request: Request, db: Session = Depends(get_db), c: Container = Depends(container),
             p: Principal = Depends(require("asset:read"))) -> dict:
    a = load_scoped(db, c, request, Asset, asset_id, p)
    tags = db.execute(select(AssetTag).where(AssetTag.asset_id == a.asset_id)
                      .order_by(AssetTag.t_offset, AssetTag.tag)).scalars()
    edits = db.execute(select(AssetTagEdit).where(AssetTagEdit.asset_id == a.asset_id)
                       .order_by(AssetTagEdit.edit_id)).scalars()
    return {"asset_id": a.asset_id,
            "tags": [{"tag": t.tag, "t_offset": None if t.t_offset < 0 else t.t_offset, "confidence": t.confidence,
                      "source": t.source, "model_ref": t.model_ref} for t in tags],
            "edits": [{"action": e.action, "tag": e.tag, "edited_by": e.edited_by, "edited_at": iso(e.edited_at)}
                      for e in edits]}


class TagEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    add: list[str] = Field(default_factory=list, max_length=50)
    remove: list[str] = Field(default_factory=list, max_length=50)


@router.patch("/v1/assets/{asset_id}/tags", tags=["deid"])
def patch_tags(asset_id: str, body: TagEdit, request: Request, db: Session = Depends(get_db),
               c: Container = Depends(container), p: Principal = Depends(require("asset:write"))) -> dict:
    bad = [t for t in [*body.add, *body.remove] if t not in tagging.VOCAB]
    if bad or not (body.add or body.remove):
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/add", "code": "vocabulary",
                                                        "message": f"태그 어휘: {', '.join(tagging.VOCAB)}"}])
    a = load_scoped(db, c, request, Asset, asset_id, p)
    tagging.edit_tags(db, a, body.add, body.remove, p.principal_id, c.clock.now())
    db.flush()
    return get_tags(asset_id, request, db, c, p)


@router.get("/v1/scenes/search", tags=["deid"])
def search_scenes(q: str = Query(min_length=1, max_length=200), limit: int = Query(20, ge=1, le=100),
                  db: Session = Depends(get_db), p: Principal = Depends(require("asset:read"))) -> dict:
    """문장으로 장면 찾기 (요구사항 7.2). 권한 범위 안의 자산만."""
    tags = tagging.text_tags(q)
    if not tags:
        raise ProblemError("VALIDATION_ERROR", errors=[{
            "pointer": "/query/q", "code": "no_vocabulary",
            "message": "아는 장면 낱말이 없습니다: " + ", ".join(sorted(tagging.KEYWORDS))}])
    qv = cast(literal(tagging.vec_literal(tagging.vector(tags))), Vector())
    dist = SE.c.embedding.op("<=>")(qv).label("d")
    rows = db.execute(select(SE.c.asset_id, SE.c.t_offset, dist).join(Asset, Asset.asset_id == SE.c.asset_id)
                      .where(SE.c.model == tagging.MODEL, scope_filter(Asset, p)).order_by(dist).limit(limit)).all()
    return {"query_tags": tags, "model": tagging.MODEL,
            "items": [{"asset_id": a, "t_offset": float(t), "score": round(1 - float(d), 4)} for a, t, d in rows]}
