"""ExternalSourcePort — 공공 Open API 등 외부 소스 (12번 스펙)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol


class ExternalSourcePort(Protocol):
    name: str

    def fetch(self, since: datetime, until: datetime) -> list[dict[str, Any]]: ...


class NotImplementedExternalSource:
    name = "unimplemented"

    def fetch(self, since: datetime, until: datetime) -> list[dict[str, Any]]:
        raise NotImplementedError("ExternalSourcePort는 12번 스펙에서 구현한다")
