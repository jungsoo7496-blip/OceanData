"""QueuePort — PostgreSQL 기반 작업 큐 (ADR-0002). 구현 선택은 05번 스펙(procrastinate PoC)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


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
    lease_s: int = 600  # 가져갈 때 건 임대 시간 (정책 표 job_policies.lease_s). 갱신할 때도 이만큼 건다


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
    # connection을 주면 호출자 트랜잭션 안에서 넣는다 (같이 쓰는 감사·상태 변경과 함께 확정·취소, 예: 구간 클립 등록)
    # dedupe_key는 기본적으로 작업이 끝난 뒤에도 같은 키의 새 작업을 막는다 (단계가 한 번만 돌게).
    # dedupe_active_only=True면 대기·실행 중(QUEUED·RUNNING)일 때만 막고, 끝난(DONE·FAILED) 작업은 키를 놓는다
    # — 다시 돌려야 하는 작업(백업, 보충 수집)용
    def enqueue(self, job_type: str, payload: dict[str, Any], *, priority: int | None = None,
                organization_id: str | None = None, dedupe_key: str | None = None,
                dedupe_active_only: bool = False, connection: Connection | None = None) -> str: ...
    def count_active(self, job_type: str, organization_id: str, *, connection: Connection | None = None) -> int: ...
    def claim(self, job_types: list[str] | None = None, worker_id: str = "",
              resource_classes: tuple[str, ...] = ("CPU",)) -> Job | None: ...
    def complete(self, job_id: str) -> None: ...
    def fail(self, job_id: str, error_code: str, retry_in_s: float | None) -> bool: ...
    # 실행 중인 작업의 임대를 지금부터 lease_s초로 다시 건다. 그 워커가 쥔 RUNNING 작업일 때만 True (ADR-0069)
    def renew_lease(self, job_id: str, worker_id: str, lease_s: int) -> bool: ...
    def reap_expired_leases(self) -> int: ...
    def depth_by_type(self) -> dict[str, int]: ...
    # 최근 것부터(created_at, job_id 역순). before=(시각, 작업 ID)를 주면 그보다 앞선 것만 — 목록 다음 페이지용
    def jobs_for_asset(self, job_type: str, asset_id: str, limit: int = 100, *,
                       before: tuple[datetime, str] | None = None) -> list[JobInfo]: ...
