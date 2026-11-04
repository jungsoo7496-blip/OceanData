from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Notification:
    kind: str
    organization_id: str | None
    subject: str
    body: str
    recipients: tuple[str, ...] = ()
    data: dict = field(default_factory=dict)


class NotifierPort(Protocol):
    def send(self, notification: Notification) -> None: ...
