"""호출 한도의 키 보관 상한과 기존 요청 창."""

from types import SimpleNamespace

import pytest

from app import ratelimit
from app.errors import ProblemError
from app.ratelimit import Limiter


def test_limiter_evicts_least_recently_used_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ratelimit, "time", SimpleNamespace(monotonic=lambda: 100.0))
    limiter = Limiter(limit=2, window_s=10, max_keys=2)
    limiter.check("keep")
    limiter.check("old")
    limiter.check("keep")  # 먼저 들어왔어도 최근 사용한 키는 남는다.
    limiter.check("new")
    assert set(limiter.hits) == {"keep", "new"}
    with pytest.raises(ProblemError) as limited:
        limiter.check("keep")
    assert limited.value.code == "RATE_LIMITED"
    for n in range(100):
        limiter.check(f"fresh-{n}")
        assert len(limiter.hits) <= 2
    assert set(limiter.hits) == {"fresh-98", "fresh-99"}


def test_limiter_preserves_per_key_window_and_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 100.0
    monkeypatch.setattr(ratelimit, "time", SimpleNamespace(monotonic=lambda: now))
    limiter = Limiter(limit=2, window_s=10)
    limiter.check("a")
    now = 101.0
    limiter.check("a")
    now = 105.0
    with pytest.raises(ProblemError) as limited:
        limiter.check("a")
    assert limited.value.code == "RATE_LIMITED"
    assert limited.value.headers["Retry-After"] == "6"
    limiter.check("b")  # 한 키가 막혀도 다른 키의 요청 창은 독립적이다.
    now = 110.5
    limiter.check("a")  # 첫 요청만 창에서 빠졌다.
    with pytest.raises(ProblemError) as limited:
        limiter.check("a")
    assert limited.value.headers["Retry-After"] == "1"
    now = 112.0
    limiter.check("a")
