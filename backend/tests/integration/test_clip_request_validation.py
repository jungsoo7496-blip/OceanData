"""09번 요구사항 7.2 구간 클립 요청 검증 보완 (검토 2026-11-12 C12, ADR-0063).

- 사유는 감사 로그에 남는다: 보이지 않는 제어·서식 문자(C1·U+0085·줄/문단 구분·방향 제어)도 400이다.
- 구간은 JSON 숫자만: 문자열 "0"·참/거짓은 400이다(전에는 0·1로 바뀌어 들어갔다).
- 촬영 시각을 모르는 원 영상의 클립은 품질 규칙(capture_time_unknown)을 받는다(전에는 PASSED).
- 클립 생성은 Idempotency-Key를 받는다: 같은 키·같은 본문은 처음 응답(Idempotent-Replayed), 다른 본문은 409.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.db.models import Asset
from tests.acceptance.test_t09_deid_review import video
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_clips import CLIPS, MEMBER_A, _clip_jobs, _source
from tests.integration.test_pipeline import drain

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg가 필요합니다")]


def _clip_path(ctx, tmp_path: Path) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    src = _source(ctx, video(tmp_path, "testsrc2=size=320x240:rate=15", "a.mp4"))
    return src, CLIPS.format(asset_id=src)


@pytest.mark.parametrize("reason", ["다음\u0085줄", "줄\u2028구분", "문단\u2029구분", "방향\u202e제어", "격리\u2066문자",
                                    "표시\u200e문자", "C1\u009b제어"],
                         ids=["NEL", "LS", "PS", "RLO", "LRI", "LRM", "CSI"])
def test_clip_reason_rejects_invisible_controls(ictx, tmp_path: Path, reason: str) -> None:  # type: ignore[no-untyped-def]
    _, path = _clip_path(ictx, tmp_path)
    r = ictx.client.post(path, json={"start_s": 1, "end_s": 2, "reason": reason}, headers=ictx.as_user(MEMBER_A))
    check(r, "POST", CLIPS)
    assert r.status_code == 400 and [e["pointer"] for e in r.json()["errors"]] == ["/reason"], r.text
    assert _clip_jobs(ictx) == []


@pytest.mark.parametrize("body", [{"start_s": "0", "end_s": 1}, {"start_s": True, "end_s": 2}, {"start_s": 0, "end_s": "2"},
                                  {"start_s": 0, "end_s": True}], ids=["str-start", "bool-start", "str-end", "bool-end"])
def test_clip_range_must_be_json_numbers(ictx, tmp_path: Path, body: dict) -> None:  # type: ignore[no-untyped-def]
    _, path = _clip_path(ictx, tmp_path)
    r = ictx.client.post(path, json=body, headers=ictx.as_user(MEMBER_A))
    check(r, "POST", CLIPS)
    assert r.status_code == 400, r.text
    assert _clip_jobs(ictx) == []
    ok = ictx.client.post(path, json={"start_s": 1, "end_s": 2}, headers=ictx.as_user(MEMBER_A))  # 정수는 숫자다
    assert ok.status_code == 202, ok.text


def test_clip_of_source_with_unknown_capture_time_gets_quality_warning(ictx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    src, path = _clip_path(ictx, tmp_path)
    with ictx.c.sessionmaker.begin() as db:
        a = db.get(Asset, src)
        a.capture_start_at = a.capture_end_at = None
        a.timestamp_quality = {"source": "UNKNOWN", "verified": False, "est_offset_ms": None}
        a.quality_status, a.quality_reasons = "WARNING", ["capture_time_unknown"]
    r = ictx.client.post(path, json={"start_s": 1, "end_s": 2}, headers=ictx.as_user(MEMBER_A))
    assert r.status_code == 202, r.text
    drain(ictx)
    items = ictx.client.get(path, headers=ictx.as_user(MEMBER_A)).json()["items"]
    assert items[0]["status"] == "DONE", items
    with ictx.c.sessionmaker() as db:
        clip = db.get(Asset, items[0]["clip_asset_id"])
        assert clip is not None and clip.capture_start_at is None
        assert (clip.quality_status, clip.quality_reasons) == ("WARNING", ["capture_time_unknown"])


def test_clip_request_honours_idempotency_key(ictx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _, path = _clip_path(ictx, tmp_path)
    h = {**ictx.as_user(MEMBER_A), "Idempotency-Key": "clip-key-0001"}
    body = {"start_s": 1, "end_s": 2, "reason": "접근 선박 구간 (합성)"}
    first = ictx.client.post(path, json=body, headers=h)
    check(first, "POST", CLIPS)
    assert first.status_code == 202 and "Idempotent-Replayed" not in first.headers, first.text
    drain(ictx)  # 작업이 끝난 뒤에도 같은 키는 새 작업을 만들지 않고 처음 응답을 준다 (키 없이 보내면 새 작업)
    again = ictx.client.post(path, json=body, headers=h)
    check(again, "POST", CLIPS)
    assert again.status_code == 202 and again.headers.get("Idempotent-Replayed") == "true", again.text
    assert again.json() == first.json()
    assert _clip_jobs(ictx) == [first.json()["job_id"]]
    assert ictx.audit_actions().count("CLIP_EXPORT_REQUESTED") == 1
    other = ictx.client.post(path, json={**body, "end_s": 3}, headers=h)
    check(other, "POST", CLIPS)
    assert other.status_code == 409 and other.json()["code"] == "IDEMPOTENCY_CONFLICT", other.text
    assert _clip_jobs(ictx) == [first.json()["job_id"]]
