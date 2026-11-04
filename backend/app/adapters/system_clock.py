from datetime import UTC, datetime


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """테스트용 고정 시계."""

    def __init__(self, at: datetime) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at

    def advance(self, **kwargs: float) -> None:
        from datetime import timedelta

        self.at = self.at + timedelta(**kwargs)
