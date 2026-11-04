"""14번 단위 시험: 가림 영역 추적·필터 문자열·표본 시각, 장면 검색 낱말 → 태그."""

from __future__ import annotations

import pytest

from app.domain.deid import Region, filter_graph, track
from app.services.deid import sample_times
from app.services.tagging import VOCAB, text_tags, vector


def test_track_bridges_short_gaps_only() -> None:
    a = Region("FACE", 0.0, 1.0, 10, 10, 20, 20)
    b = Region("FACE", 1.3, 2.0, 14, 12, 20, 20)
    far = Region("FACE", 5.0, 6.0, 14, 12, 20, 20)
    other = Region("LICENSE_PLATE", 1.2, 2.0, 10, 10, 20, 20)
    out = track([a, b, far, other], max_gap_s=0.5)
    faces = [r for r in out if r.cls == "FACE"]
    assert [(r.t0, r.t1) for r in faces] == [(0.0, 2.0), (5.0, 6.0)]
    assert faces[0].w == 24 and faces[0].h == 22  # 두 영역을 감싼다
    assert len([r for r in out if r.cls == "LICENSE_PLATE"]) == 1


@pytest.mark.parametrize("method", ["BLUR", "MOSAIC", "FILL"])
def test_filter_graph(method: str) -> None:
    g = filter_graph([Region("FACE", 0.5, 1.5, 300, 200, 100, 100)], method, 320, 240)
    assert g.endswith("[out]") and "between(t,0.500,1.500)" in g
    assert "crop=20:40:300:200" in g or "w=20:h=40" in g  # 화면 밖은 잘라 낸다 (짝수 크기)
    assert filter_graph([], method, 320, 240) == "[0:v]null[out]"
    with pytest.raises(ValueError):
        filter_graph([], "PIXEL", 320, 240)


def test_sample_times() -> None:
    assert sample_times(10, 0.05, 3) == [1.667, 5.0, 8.333]
    assert len(sample_times(10, 1.0, 10)) == 10
    assert sample_times(0, 1.0, 5) == [0.0]


def test_text_tags_and_vector() -> None:
    assert text_tags("해무 속 소형 선박") == ["FOG", "VESSEL_SMALL"]
    assert text_tags("밤에 지나가는 배") == ["NIGHT", "VESSEL_LARGE", "VESSEL_MEDIUM", "VESSEL_SMALL"]
    assert text_tags("hello") == []
    v = vector({"NIGHT", "BUOY"})
    assert len(v) == len(VOCAB) == 16 and abs(sum(x * x for x in v) - 1) < 1e-9
