"""14번 요구사항 6.3: 결합된 관측값(12번)으로 날씨 태그 보정 — 추가·영상 모델 오탐 제거·사람 태그 유지·이력."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import Asset, AssetTag, AssetTagEdit
from app.services import tagging
from tests.acceptance.test_t09_deid_review import original, video

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg 필요")]
NOW = datetime(2026, 11, 5, tzinfo=UTC)


def _env(**metrics: float) -> dict:
    return {"joined_at": "2026-11-05T00:00:00Z", "radius_km": 30, "window_min": 30,
            "observations": [{"metric": k, "value": v, "unit": "m", "station_name": "시험 부이 1", "distance_km": 1.0,
                              "time_diff_s": 60, "attribution": "모의", "redistribution_allowed": False}
                             for k, v in metrics.items()]}


def _tags(ctx, aid: str) -> dict[str, tuple[str, str | None]]:  # type: ignore[no-untyped-def]
    with ctx.c.sessionmaker() as db:
        return {t.tag: (t.source, t.model_ref) for t in db.execute(select(AssetTag).where(AssetTag.asset_id == aid))
                .scalars()}


def test_weather_tags_follow_observations(ictx, tmp_path: Path) -> None:
    ctx = ictx
    data = video(tmp_path, "testsrc2=size=320x240:rate=15", "w.mp4")
    # 영상 모델이 '높은 파도'라고 했지만 관측은 잔잔함, 시정은 300m(안개) — 환경 정보가 먼저 붙어 있는 경우
    stormy_by_model = original(ctx, data, synthetic_objects=[{"tag": "HIGH_WAVE", "t0": 0, "t1": 2, "confidence": 0.7}])
    with ctx.c.sessionmaker.begin() as db:
        db.get(Asset, stormy_by_model).env_context = _env(significant_wave_height=0.4, visibility=300)
    assert tagging.tag_scenes(ctx.c, stormy_by_model) == "OK"
    tags = _tags(ctx, stormy_by_model)
    assert tags.get("FOG") == ("MODEL", tagging.WEATHER_MODEL) and "HIGH_WAVE" not in tags
    with ctx.c.sessionmaker() as db:
        edits = list(db.execute(select(AssetTagEdit).where(AssetTagEdit.asset_id == stormy_by_model)).scalars())
        assert [(e.action, e.tag, e.edited_by) for e in edits] == [("REMOVE", "HIGH_WAVE", tagging.WEATHER_MODEL)]

    # 환경 정보가 나중에 붙는 경우 (env_join): 파고 3.5m → HIGH_WAVE, 시정 8km → 모델의 FOG 제거, 사람 태그는 유지
    later = original(ctx, video(tmp_path, "testsrc2=size=320x240:rate=15:duration=2", "w2.mp4"),
                     synthetic_objects=[{"tag": "FOG", "t0": 0, "t1": 1, "confidence": 0.6}])
    assert tagging.tag_scenes(ctx.c, later) == "OK"
    with ctx.c.sessionmaker.begin() as db:
        db.add(AssetTag(asset_id=later, organization_id=db.get(Asset, later).organization_id, tag="RAIN",
                        t_offset=-1.0, source="HUMAN", created_at=NOW))
    with ctx.c.sessionmaker.begin() as db:
        a = db.get(Asset, later)
        a.env_context = _env(significant_wave_height=3.5, visibility=8000)
        out = tagging.apply_weather(db, ctx.c.settings, a, NOW)
    assert out == {"added": ["HIGH_WAVE"], "removed": ["FOG"]}
    tags = _tags(ctx, later)
    assert "FOG" not in tags and tags["HIGH_WAVE"] == ("MODEL", tagging.WEATHER_MODEL)
    assert tags["RAIN"] == ("HUMAN", None)  # 강수는 관측 항목이 없어 보정하지 않고, 사람 태그는 건드리지 않는다
    with ctx.c.sessionmaker.begin() as db:  # 다시 적용해도 그대로 (중복 없음)
        assert tagging.apply_weather(db, ctx.c.settings, db.get(Asset, later), NOW) == {"added": [], "removed": []}
    # 관측이 경계 사이(파고 2m·시정 3km)면 어느 쪽으로도 바꾸지 않는다
    with ctx.c.sessionmaker.begin() as db:
        a = db.get(Asset, later)
        a.env_context = _env(significant_wave_height=2.0, visibility=3000)
        assert tagging.apply_weather(db, ctx.c.settings, a, NOW) == {"added": [], "removed": []}
