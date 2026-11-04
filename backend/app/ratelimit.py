"""호출 제한 (04번 요구사항 9, tech.md 2장: 개발 단계는 FastAPI 안에서, 통합 단계는 게이트웨이).

프로세스 메모리 기준이라 API를 여러 대로 늘리면 게이트웨이(APISIX 등)에서 제한한다.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from app.errors import ProblemError


class Limiter:
    def __init__(self, limit: int, window_s: float = 60.0) -> None:
        self.limit, self.window_s = limit, window_s
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            q = self.hits[key]
            while q and now - q[0] > self.window_s:
                q.popleft()
            if len(q) >= self.limit:
                retry = int(self.window_s - (now - q[0])) + 1
                raise ProblemError("RATE_LIMITED", headers={"Retry-After": str(retry)})
            q.append(now)
