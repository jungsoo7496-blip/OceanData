"""요청의 DB 작업 동시 실행 수 (ADR-0062)."""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import anyio.to_thread

from app.errors import ProblemError
from app.metrics import DB_SLOT_TIMEOUTS


class RequestSlots:
    """요청이 DB 작업에 들어가기 전에 받는 차례.

    동기 의존성·경로 함수는 요청마다 스레드 풀(기본 40)을 여러 번 오가며(인증 → 권한 → 경로 → 커밋) 그동안 DB 연결을
    쥔다. 동시 요청이 스레드 한도를 넘으면 스레드는 연결을 기다리는 요청이, 연결은 다음 스레드 차례를 기다리는 요청이
    차지해 서로 막혔다(연결 대기 시간이 지나 500). 차례는 이벤트 루프에서 비동기로 기다리므로 기다리는 요청은 스레드도
    연결도 쥐지 않는다. 차례 수를 연결 풀과 스레드 한도보다 작게 두면 차례를 받은 요청은 연결과 스레드를 얻는다.

    차례를 wait_s 안에 받지 못하면 503 SERVICE_UNAVAILABLE(Retry-After)이다."""

    def __init__(self, total: int, *, wait_s: float) -> None:
        self.total, self.wait_s = total, wait_s
        # 차례 표는 이벤트 루프마다 따로 둔다 (시험은 루프를 여러 번 만든다)
        self._by_loop: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, anyio.CapacityLimiter] = (
            weakref.WeakKeyDictionary())

    def limiter(self) -> anyio.CapacityLimiter:
        loop = asyncio.get_running_loop()
        limiter = self._by_loop.get(loop)
        if limiter is None:
            threads = int(anyio.to_thread.current_default_thread_limiter().total_tokens)
            # 차례를 받은 요청이 스레드를 못 얻는 일이 없게 스레드 한도보다 작게 둔다
            limiter = self._by_loop[loop] = anyio.CapacityLimiter(max(1, min(self.total, threads - 1)))
        return limiter

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        limiter, token = self.limiter(), object()  # 차례를 잡은 쪽을 따로 표시한다 (마무리가 다른 태스크여도 된다)
        acquired = False
        with anyio.move_on_after(self.wait_s):
            await limiter.acquire_on_behalf_of(token)
            acquired = True
        if not acquired:
            DB_SLOT_TIMEOUTS.inc()
            raise ProblemError("SERVICE_UNAVAILABLE", headers={"Retry-After": "1"},
                               detail="요청이 몰려 처리를 시작하지 못했습니다. 잠시 뒤 다시 시도하세요")
        try:
            yield
        finally:
            limiter.release_on_behalf_of(token)
