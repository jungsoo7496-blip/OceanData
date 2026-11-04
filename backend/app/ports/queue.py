"""QueuePort — PostgreSQL 기반 작업 큐 (ADR-0002). 구현 선택은 05번 스펙(procrastinate PoC)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass
class Job:
    job_id: str
    job_type: str
    payload: dict[str, Any]
    attempts: int = 0
    priority: int = 0
    organization_id: str | None = None
    created_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class QueuePort(Protocol):
    def enqueue(self, job_type: str, payload: dict[str, Any], *, priority: int = 0,
                organization_id: str | None = None, dedupe_key: str | None = None) -> str: ...
    def claim(self, job_types: list[str] | None = None, worker_id: str = "") -> Job | None: ...
    def complete(self, job_id: str) -> None: ...
    def fail(self, job_id: str, error_code: str, retry_in_s: float | None) -> None: ...
    def depth_by_type(self) -> dict[str, int]: ...
