from datetime import datetime
from typing import Protocol


class ClockPort(Protocol):
    def now(self) -> datetime:
        """현재 UTC 시각 (tz-aware)."""
        ...
