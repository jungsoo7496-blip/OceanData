"""사람 태그 수정 (14번 요구사항 6.2·6.3, 계약 점검 S21).

모델이 단 자산 전체 태그를 사람이 추가(확인)하면 그 태그는 source=HUMAN이 되고, 뒤에 날씨 보정이 다시 돌아도 지우지 않는다.
합성 자산과 합성 관측값만 쓴다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.db.models import Asset
from app.seed import DEV_A_CAMERA, ORG_A, PRJ_A
from app.services import tagging
from tests.integration.test_catalog import _asset

pytestmark = [pytest.mark.integration]
MEMBER = 102
T0 = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)


def _weather(ctx: Any, aid: str, visibility_m: float) -> dict[str, list[str]]:
    with ctx.c.sessionmaker.begin() as db:
        a = db.get(Asset, aid)
        a.env_context = {"observations": [{"metric": "visibility", "value": visibility_m}]}
        return tagging.apply_weather(db, ctx.c.settings, a, T0)


def test_human_add_of_model_tag_is_kept_by_weather_correction(ctx) -> None:
    with ctx.c.sessionmaker.begin() as db:
        a = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
        db.add(a)
        aid = a.asset_id
    assert _weather(ctx, aid, 300.0) == {"added": ["FOG"], "removed": []}  # 해무: 보정이 FOG(MODEL)를 단다

    r = ctx.client.patch(f"/v1/assets/{aid}/tags", json={"add": ["FOG"]}, headers=ctx.as_user(MEMBER))
    assert r.status_code == 200
    assert [(t["tag"], t["t_offset"], t["source"], t["model_ref"], t["confidence"]) for t in r.json()["tags"]] == [
        ("FOG", None, "HUMAN", None, None)]  # 사람이 확인한 태그 — 이력과 상태가 같은 말을 한다

    assert _weather(ctx, aid, 20000.0) == {"added": [], "removed": []}  # 맑음으로 다시 결합돼도 사람 태그는 그대로
    body = ctx.client.get(f"/v1/assets/{aid}/tags", headers=ctx.as_user(MEMBER)).json()
    assert [(t["tag"], t["source"]) for t in body["tags"]] == [("FOG", "HUMAN")]
    assert [(e["action"], e["tag"], e["edited_by"].startswith("usr_")) for e in body["edits"]] == [
        ("ADD", "FOG", True)]
