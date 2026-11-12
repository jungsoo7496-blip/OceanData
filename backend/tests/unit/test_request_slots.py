"""요청의 DB 작업 차례 (ADR-0062): 한도까지만 동시에 들어가고, 기다리다 시간이 지나면 503 SERVICE_UNAVAILABLE."""

from __future__ import annotations

import anyio
import pytest

from app.db.slots import RequestSlots
from app.errors import ProblemError


def test_slots_admit_up_to_total_and_time_out_with_503() -> None:
    slots = RequestSlots(2, wait_s=0.2)
    seen: dict[str, object] = {}

    async def main() -> None:
        inside = 0
        peak = 0
        release = anyio.Event()

        async def holder() -> None:
            nonlocal inside, peak
            async with slots.hold():
                inside += 1
                peak = max(peak, inside)
                await release.wait()
                inside -= 1

        async with anyio.create_task_group() as tg:
            tg.start_soon(holder)
            tg.start_soon(holder)
            await anyio.sleep(0.05)
            with pytest.raises(ProblemError) as late:  # 두 자리가 찼다 — 세 번째는 기다리다 503
                async with slots.hold():
                    pass
            seen["late"] = late.value
            release.set()
        async with slots.hold():  # 자리가 풀리면 다시 들어간다
            seen["after"] = True
        seen["peak"] = peak

    anyio.run(main)
    late = seen["late"]
    assert isinstance(late, ProblemError) and late.code == "SERVICE_UNAVAILABLE"
    assert late.headers == {"Retry-After": "1"}
    assert seen["peak"] == 2 and seen["after"] is True


def test_slots_stay_below_thread_limit() -> None:
    async def main() -> float:
        return RequestSlots(1000, wait_s=1).limiter().total_tokens

    assert anyio.run(main) < 40  # 기본 스레드 한도(40)보다 작다 — 차례를 받은 요청은 스레드를 얻는다


def test_empty_optional_numbers_mean_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """.env.example·Compose의 빈 값(`DB_REQUEST_SLOTS=`, `STORAGE_CAPACITY_BYTES=`)으로도 기동한다."""
    from tests.conftest import test_settings

    monkeypatch.setenv("DB_REQUEST_SLOTS", "")
    monkeypatch.setenv("STORAGE_CAPACITY_BYTES", " ")
    s = test_settings()
    assert s.db_request_slots is None and s.storage_capacity_bytes is None


def test_default_slots_follow_pool_settings() -> None:
    from app.container import build_container
    from tests.conftest import FakeStorage, StaticIdentity, test_settings

    c = build_container(test_settings(db_pool_size=4, db_max_overflow=6), storage=FakeStorage(),
                        identity=StaticIdentity())
    try:
        assert c.db_slots.total == 5  # (풀 4 + 초과 6) // 2 — 요청 하나가 연결을 둘까지 쥘 수 있다
        assert (c.engine.pool.size(), c.engine.pool.timeout()) == (4, 30.0)
    finally:
        c.engine.dispose()
