"""QueuePort → PostgreSQL `FOR UPDATE SKIP LOCKED` (ADR-0002, ADR-0014).

- 우선순위·자원 클래스·최대 시도·임대 시간은 `job_policies` 표에서 읽는다 (05번 요구사항 6, HW-3)
- 임대(lease)가 지난 RUNNING 작업은 `reap_expired_leases()`가 다시 QUEUED로 돌린다 (요구사항 1.3)
- 외부 브로커는 쓰지 않는다
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.domain.ids import new_id
from app.ports.clock import ClockPort
from app.ports.queue import Job, JobInfo

DEFAULT_POLICY = {"priority": 0, "resource_class": "CPU", "gpu_fallback": "CPU", "max_attempts": 5, "lease_s": 600}


class PgQueue:
    def __init__(self, engine: Engine, clock: ClockPort, max_attempts: int = 5) -> None:
        self.engine = engine
        self.clock = clock
        self.max_attempts = max_attempts

    def _policy(self, conn: Any, job_type: str) -> dict[str, Any]:
        row = conn.execute(text("""SELECT priority, resource_class, gpu_fallback, max_attempts, lease_s
                                   FROM job_policies WHERE job_type = :t"""), {"t": job_type}).mappings().first()
        return dict(row) if row else {**DEFAULT_POLICY, "max_attempts": self.max_attempts}

    def enqueue(self, job_type: str, payload: dict[str, Any], *, priority: int | None = None,
                organization_id: str | None = None, dedupe_key: str | None = None) -> str:
        job_id = new_id("job")
        now = self.clock.now()
        with self.engine.begin() as conn:
            pol = self._policy(conn, job_type)
            row = conn.execute(
                text("""
                INSERT INTO jobs (job_id, job_type, payload, status, priority, attempts, max_attempts, run_after,
                                  organization_id, dedupe_key, asset_id, resource_class, created_at, updated_at)
                VALUES (:id, :t, CAST(:p AS JSONB), 'QUEUED', :prio, 0, :max, :now, :org, :dk, :asset, :rc, :now, :now)
                ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
                RETURNING job_id
                """),
                {"id": job_id, "t": job_type, "p": json.dumps(payload),
                 "prio": pol["priority"] if priority is None else priority, "max": pol["max_attempts"], "now": now,
                 "org": organization_id, "dk": dedupe_key, "asset": payload.get("asset_id"),
                 "rc": pol["resource_class"]},
            ).first()
            if row is None:  # 같은 dedupe_key 작업이 이미 있다
                existing = conn.execute(text("SELECT job_id FROM jobs WHERE dedupe_key = :dk"),
                                        {"dk": dedupe_key}).scalar_one()
                return str(existing)
        return job_id

    def claim(self, job_types: list[str] | None = None, worker_id: str = "",
              resource_classes: tuple[str, ...] = ("CPU",)) -> Job | None:
        """worker가 가진 자원 클래스로 실행할 수 있는 작업만 가져간다.

        GPU 작업은 GPU 워커가 가져가고, GPU 워커가 없으면 정책(gpu_fallback=CPU)에 따라 CPU 워커가 실행한다.
        """
        now = self.clock.now()
        has_gpu = "GPU" in resource_classes
        with self.engine.begin() as conn:
            row = conn.execute(
                text("""
                SELECT j.job_id, j.job_type, j.payload, j.attempts, j.priority, j.organization_id, j.created_at,
                       coalesce(p.lease_s, 600) AS lease_s
                FROM jobs j LEFT JOIN job_policies p ON p.job_type = j.job_type
                WHERE j.status = 'QUEUED' AND j.run_after <= :now
                  AND (CAST(:any_type AS BOOLEAN) OR j.job_type = ANY(:types))
                  AND (j.resource_class = 'CPU' OR CAST(:has_gpu AS BOOLEAN)
                       OR coalesce(p.gpu_fallback, 'CPU') = 'CPU')
                ORDER BY j.priority DESC, j.run_after, j.created_at
                FOR UPDATE OF j SKIP LOCKED
                LIMIT 1
                """),
                {"now": now, "any_type": not job_types, "types": job_types or [], "has_gpu": has_gpu},
            ).mappings().first()
            if row is None:
                return None
            conn.execute(
                text("""UPDATE jobs SET status='RUNNING', attempts = attempts + 1, locked_by=:w, locked_at=:now,
                        lease_expires_at=:lease, updated_at=:now WHERE job_id=:id"""),
                {"w": worker_id, "now": now, "id": row["job_id"],
                 "lease": now + timedelta(seconds=int(row["lease_s"]))},
            )
        payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
        return Job(job_id=row["job_id"], job_type=row["job_type"], payload=payload,
                   attempts=int(row["attempts"]) + 1, priority=int(row["priority"]),
                   organization_id=row["organization_id"], created_at=row["created_at"])

    def complete(self, job_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("""UPDATE jobs SET status='DONE', locked_by=NULL, lease_expires_at=NULL, updated_at=:now
                                 WHERE job_id=:id"""), {"now": self.clock.now(), "id": job_id})

    def fail(self, job_id: str, error_code: str, retry_in_s: float | None) -> bool:
        """재시도를 예약하면 True, 최대 시도를 넘겨 FAILED가 되면 False."""
        now = self.clock.now()
        with self.engine.begin() as conn:
            attempts, max_attempts = conn.execute(
                text("SELECT attempts, max_attempts FROM jobs WHERE job_id=:id FOR UPDATE"), {"id": job_id}
            ).one()
            if retry_in_s is not None and attempts < max_attempts:
                conn.execute(
                    text("""UPDATE jobs SET status='QUEUED', run_after=:ra, last_error_code=:e, locked_by=NULL,
                            lease_expires_at=NULL, updated_at=:now WHERE job_id=:id"""),
                    {"ra": now + timedelta(seconds=retry_in_s), "e": error_code, "now": now, "id": job_id},
                )
                return True
            conn.execute(
                text("""UPDATE jobs SET status='FAILED', last_error_code=:e, locked_by=NULL, lease_expires_at=NULL,
                        updated_at=:now WHERE job_id=:id"""),
                {"e": error_code, "now": now, "id": job_id},
            )
            return False

    def reap_expired_leases(self) -> int:
        """작업 도중 멈춘 워커의 작업을 다시 대기 상태로 (요구사항 1.3)."""
        now = self.clock.now()
        with self.engine.begin() as conn:
            res = conn.execute(text("""UPDATE jobs SET status='QUEUED', locked_by=NULL, lease_expires_at=NULL,
                                       last_error_code='LEASE_EXPIRED', run_after=:now, updated_at=:now
                                       WHERE status='RUNNING' AND lease_expires_at < :now"""), {"now": now})
            return int(res.rowcount or 0)

    def depth_by_type(self) -> dict[str, int]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT job_type, count(*) FROM jobs WHERE status='QUEUED' GROUP BY job_type")
            ).all()
        return {r[0]: int(r[1]) for r in rows}

    def jobs_for_asset(self, job_type: str, asset_id: str, limit: int = 100) -> list[JobInfo]:
        """한 자산에 걸린 작업을 최근 것부터 (예: 구간 클립 진행 상황, 09번 요구사항 7.2)."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                text("""SELECT job_id, job_type, status, payload, attempts, last_error_code, created_at, updated_at
                        FROM jobs WHERE job_type = :t AND asset_id = :a
                        ORDER BY created_at DESC, job_id DESC LIMIT :n"""),
                {"t": job_type, "a": asset_id, "n": limit},
            ).mappings().all()
        return [JobInfo(**{**dict(r), "attempts": int(r["attempts"]),
                           "payload": r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"])})
                for r in rows]
