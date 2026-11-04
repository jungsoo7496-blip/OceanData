"""샘플링 태깅·장면 임베딩·장면 검색 (14번 요구사항 6, 7).

기준선(baseline) 구현 — 학습 모델 없이 동작하고, 모델을 Model_Registry에 등록하면 같은 자리를 바꾼다 (확인 필요):
- 주야: 표본 프레임(정책 fps)의 평균 밝기로 DAY/NIGHT.
- 객체: DetectorPort와 같은 방식의 모의 입력(asset.ext["synthetic_objects"], 합성 시험용).
- 임베딩: 태그 어휘(16차원) 벡터. 문장 검색은 한국어 낱말을 어휘로 바꿔 같은 공간에서 코사인 거리로 찾는다.
"""

from __future__ import annotations

import logging
import math
import re
import subprocess  # nosec B404 - 고정된 ffmpeg 실행 파일만 인자 목록으로 호출한다
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import Asset, AssetTag, AssetTagEdit, Event, EventAsset, TagEventRule
from app.domain.ids import new_id

if TYPE_CHECKING:
    from app.container import Container

log = logging.getLogger(__name__)
MODEL = "tag-vocab-baseline@1"
VOCAB = ("DAY", "NIGHT", "VESSEL_SMALL", "VESSEL_MEDIUM", "VESSEL_LARGE", "BUOY", "FLOATING_OBJECT", "PERSON",
         "VEHICLE", "FOG", "RAIN", "HIGH_WAVE", "SHORE", "BIRD", "LIGHT", "OTHER")
KEYWORDS: dict[str, tuple[str, ...]] = {
    "야간": ("NIGHT",), "밤": ("NIGHT",), "주간": ("DAY",), "낮": ("DAY",),
    "소형": ("VESSEL_SMALL",), "중형": ("VESSEL_MEDIUM",), "대형": ("VESSEL_LARGE",),
    "선박": ("VESSEL_SMALL", "VESSEL_MEDIUM", "VESSEL_LARGE"), "배": ("VESSEL_SMALL", "VESSEL_MEDIUM", "VESSEL_LARGE"),
    "어선": ("VESSEL_SMALL",), "부표": ("BUOY",), "부유물": ("FLOATING_OBJECT",), "쓰레기": ("FLOATING_OBJECT",),
    "사람": ("PERSON",), "차량": ("VEHICLE",), "해무": ("FOG",), "안개": ("FOG",), "비": ("RAIN",),
    "파도": ("HIGH_WAVE",), "해안": ("SHORE",), "새": ("BIRD",), "조명": ("LIGHT",),
}
OBJECT_TAGS = frozenset(VOCAB) - {"DAY", "NIGHT"}


def text_tags(query: str) -> list[str]:
    """문장 → 어휘 태그. 크기 낱말(소형 등)이 있으면 '선박'은 그 크기로만 좁힌다."""
    words = re.findall(r"[가-힣A-Za-z]+", query)
    tags: list[str] = []
    for w in words:
        for k, v in KEYWORDS.items():
            if k in w:
                tags += v
    sizes = {t for t in tags if t in ("VESSEL_SMALL", "VESSEL_MEDIUM", "VESSEL_LARGE")}
    explicit = {KEYWORDS[k][0] for k in ("소형", "중형", "대형", "어선") if any(k in w for w in words)}
    if explicit:
        tags = [t for t in tags if t not in sizes or t in explicit]
    return sorted(set(tags))


def vector(tags: list[str] | set[str]) -> list[float]:
    v = [1.0 if t in tags else 0.0 for t in VOCAB]
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


def vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def _luma(c: Container, path: str, fps: float) -> list[float]:
    """표본 프레임 평균 밝기 (32×18 회색조로 줄여 계산)."""
    args = [c.settings.ffmpeg_path, "-hide_banner", "-loglevel", "error", "-i", path, "-vf",
            f"fps={fps},scale=32:18,format=gray", "-f", "rawvideo", "-"]
    r = subprocess.run(args, capture_output=True, timeout=c.settings.ffmpeg_timeout_s, check=False)  # noqa: S603  # nosec B603
    data, size = r.stdout, 32 * 18
    return [sum(data[i:i + size]) / size for i in range(0, len(data) - size + 1, size)]


def tag_scenes(c: Container, asset_id: str) -> str:
    """요구사항 6.1, 6.4, 7.1: 표본 태그 저장, 이벤트 규칙, 장면 임베딩."""
    from app.services.pipeline import local_copy

    s = c.settings
    with c.sessionmaker() as db:
        a = db.get(Asset, asset_id)
        if a is None or a.data_type != "VIDEO" or not a.has_binary or a.deleted_at is not None:
            return "SKIPPED"
        db.expunge(a)
    with local_copy(c, a) as src:
        lumas = _luma(c, src, s.tag_sample_fps)
    now = c.clock.now()
    frames: list[tuple[float, set[str]]] = []
    for i, y in enumerate(lumas):
        t = round((i + 0.5) / s.tag_sample_fps, 3)
        frames.append((t, {"NIGHT" if y < s.night_luma_threshold else "DAY"}))
    objects = [o for o in (a.ext or {}).get("synthetic_objects", []) if o.get("tag") in OBJECT_TAGS]
    for t, tags in frames:
        tags |= {o["tag"] for o in objects if float(o["t0"]) <= t <= float(o["t1"])}
    with c.sessionmaker.begin() as db:
        night = sum(1 for _, tg in frames if "NIGHT" in tg)
        rows: list[dict[str, Any]] = []
        if frames:
            share = night / len(frames)
            whole = "NIGHT" if share >= 0.5 else "DAY"
            rows.append({"tag": whole, "t_offset": -1.0,
                         "confidence": round(share if whole == "NIGHT" else 1 - share, 3)})
        for o in objects:
            rows.append({"tag": o["tag"], "t_offset": float(o["t0"]), "confidence": float(o.get("confidence", 0.9))})
        for r in rows:
            db.execute(insert(AssetTag).values(asset_id=a.asset_id, organization_id=a.organization_id,
                                               source="MODEL", model_ref=MODEL, created_at=now, **r)
                       .on_conflict_do_nothing())
        _events_from_tags(db, a, objects, now)
        last = -1e9
        for t, tags in frames:  # 정책 간격마다 한 벡터
            if t - last + 1e-9 < s.embed_interval_s:
                continue
            last = t
            db.execute(text("""INSERT INTO scene_embeddings (asset_id, organization_id, t_offset, model, embedding,
                                                             created_at)
                               VALUES (:a, :o, :t, :m, CAST(:v AS vector), :now) ON CONFLICT DO NOTHING"""),
                       {"a": a.asset_id, "o": a.organization_id, "t": t, "m": MODEL,
                        "v": vec_literal(vector(tags)), "now": now})
    return "OK"


def _events_from_tags(db: Session, a: Asset, objects: list[dict[str, Any]], now: datetime) -> None:
    """정책 표의 규칙에 맞는 탐지 → source=MODEL, human_verified=false 이벤트 (요구사항 6.4)."""
    if not a.session_id or not a.capture_start_at:
        return
    rules = {r.tag: r for r in db.execute(select(TagEventRule).where(TagEventRule.enabled)).scalars()}
    from datetime import timedelta

    for o in objects:
        rule = rules.get(o["tag"])
        conf = float(o.get("confidence", 0.9))
        if rule is None or conf < rule.min_confidence:
            continue
        sid = f"tag:{a.asset_id}:{o['tag']}:{o['t0']}"
        exists = db.execute(select(Event.event_id).where(Event.organization_id == a.organization_id,
                                                         Event.source_system_id == a.source_system_id,
                                                         Event.source_event_id == sid)).scalar()
        if exists:
            continue
        e = Event(event_id=new_id("event"), organization_id=a.organization_id, session_id=a.session_id,
                  project_id=a.project_id, source_system_id=a.source_system_id, event_code=rule.event_code,
                  started_at=a.capture_start_at + timedelta(seconds=float(o["t0"])),
                  ended_at=a.capture_start_at + timedelta(seconds=float(o["t1"])),
                  timestamp_quality=a.timestamp_quality, severity="INFO", source="MODEL", model_ref=MODEL,
                  confidence=conf, human_verified=False, source_event_id=sid, schema_version="1.0.0",
                  created_by="SERVICE", created_at=now)
        db.add(e)
        db.flush()
        db.add(EventAsset(event_id=e.event_id, asset_id=a.asset_id))


def edit_tags(db: Session, a: Asset, add: list[str], remove: list[str], by: str, now: datetime) -> None:
    """사람 수정과 이력 (요구사항 6.2)."""
    for t in add:
        db.execute(insert(AssetTag).values(asset_id=a.asset_id, organization_id=a.organization_id, tag=t,
                                           t_offset=-1.0, confidence=None, source="HUMAN", created_at=now)
                   .on_conflict_do_nothing())
        db.add(AssetTagEdit(asset_id=a.asset_id, action="ADD", tag=t, edited_by=by, edited_at=now))
    for t in remove:
        for row in db.execute(select(AssetTag).where(AssetTag.asset_id == a.asset_id, AssetTag.tag == t)).scalars():
            db.delete(row)
        db.add(AssetTagEdit(asset_id=a.asset_id, action="REMOVE", tag=t, edited_by=by, edited_at=now))
