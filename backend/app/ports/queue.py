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


@dataclass
class JobInfo:
    """작업 상태 조회용 (QUEUED | RUNNING | DONE | FAILED). 화면에 진행 상황을 보여 줄 때 쓴다."""
    job_id: str
    job_type: str
    status: str
    payload: dict[str, Any]
    attempts: int
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime


class PermanentJobError(Exception):
    """다시 해도 같은 결과인 실패 — 재시도하지 않고 바로 FAILED. code는 jobs.last_error_code에 남는다."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class QueuePort(Protocol):
    def enqueue(self, job_type: str, payload: dict[str, Any], *, priority: int | None = None,
                organization_id: str | None = None, dedupe_key: str | None = None) -> str: ...
    def claim(self, job_types: list[str] | None = None, worker_id: str = "",
              resource_classes: tuple[str, ...] = ("CPU",)) -> Job | None: ...
    def complete(self, job_id: str) -> None: ...
    def fail(self, job_id: str, error_code: str, retry_in_s: float | None) -> bool: ...
    def reap_expired_leases(self) -> int: ...
    def depth_by_type(self) -> dict[str, int]: ...
    def jobs_for_asset(self, job_type: str, asset_id: str, limit: int = 100) -> list[JobInfo]: ...
