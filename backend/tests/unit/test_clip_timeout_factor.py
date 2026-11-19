"""통합 검토 CLIPS-3·CLIPS-4: CLIP_TIMEOUT_FACTOR가 inf·아주 큰 값이어도 클립 작업이 OverflowError로 죽지 않는다 (ADR-0069).

inf·nan은 설정을 읽을 때 막아 워커가 기동하지 않고(작업 유형 설정 오류와 같다), 유한하지만 구간 길이와 곱하면
float 범위를 넘는 값은 전체 한도(FFMPEG_TIMEOUT_S)로 자른다 — 전에는 math.ceil(inf)가 OverflowError를 내
클립 작업마다 INTERNAL로 세 번 재시도한 뒤 FAILED가 됐다.
"""

from __future__ import annotations

import pytest

from app.config import ConfigError
from app.services.clips import clip_timeout_s
from tests import conftest


@pytest.mark.parametrize("raw", ["inf", "Infinity", "-inf", "nan"])
def test_non_finite_factor_stops_startup_with_key_name(raw: str) -> None:
    with pytest.raises(ConfigError) as e:
        conftest.test_settings(clip_timeout_factor=raw)
    assert e.value.invalid == ["CLIP_TIMEOUT_FACTOR"]


@pytest.mark.parametrize("raw", ["1e308", "1.7e308"])
def test_huge_factor_is_capped_by_global_limit(raw: str) -> None:
    s = conftest.test_settings(clip_timeout_factor=raw)
    assert s.ffmpeg_timeout_s == 3600
    assert clip_timeout_s(s, 3600.0) == 3600  # 곱이 inf로 넘쳐도 전체 한도
    assert clip_timeout_s(s, 0.001) == 3600


def test_limit_for_normal_factors_is_unchanged() -> None:
    """기본값(배수 4·최소 120초·전체 3600초)의 한도는 그대로다."""
    s = conftest.test_settings()
    lengths = (0.04, 1, 30, 30.1, 60, 899.5, 900, 3600)
    assert [clip_timeout_s(s, x) for x in lengths] == [120, 120, 120, 121, 240, 3598, 3600, 3600]
