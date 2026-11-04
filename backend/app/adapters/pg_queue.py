"""QueuePort → PostgreSQL `FOR UPDATE SKIP LOCKED` (ADR-0002).

05번 스펙에서 procrastinate PoC 결과에 따라 교체할 수 있다. 외부 브로커는 쓰지 않는다.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.domain.ids import new_id
from app.ports.clock import ClockPort
from app.ports.queue import Job


class PgQueue:
    def __init__(self, engine: Engine, clock: ClockPort, max_attempts: int = 5) -> None:
        self.engine = engine
        self.clock = clock
        self.max_attempts = max_attempts

    def enqueue(self, job_type: str, payload: dict[str, Any], *, priority: int = 0,
                organization_id: str | None = None, dedupe_key: str | None = None) -> str:
        job_id = new_id("job")
        now = self.clock.now()
        with self.engine.begin() as conn:
            row = conn.execute(
                text("""
                INSERT INTO jobs (job_id, job_type, payload, status, priority, attempts, max_attempts,
                                  run_after, organization_id, dedupe_key, created_at, updated_at)
                VALUES (:id, :t, CAST(:p AS JSONB), 'QUEUED', :prio, 0, :max, :now, :org, :dk, :now, :now)
                ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
                RETURNING job_id
                """),
                {"id": job_id, "t": job_type, "p": json.dumps(payload), "prio": priority,
                 "max": self.max_attempts, "now": now, "org": organization_id, "dk": dedupe_key},
            ).first()
            if row is None:  # 같은 dedupe_key 작업이 이미 있다
                existing = conn.execute(text("SELECT job_id FROM jobs WHERE dedupe_key = :dk"),
                                        {"dk": dedupe_key}).scalar_one()
                return str(existing)
        return job_id

    def claim(self, job_types: list[str] | None = None, worker_id: str = "") -> Job | None:
        now = self.clock.now()
        with self.engine.begin() as conn:
            row = conn.execute(
                text("""
                SELECT job_id, job_type, payload, attempts, priority, organization_id, created_at
                FROM jobs
                WHERE status = 'QUEUED' AND run_after <= :now
                  AND (CAST(:any_type AS BOOLEAN) OR job_type = ANY(:types))
                ORDER BY priority DESC, run_after, created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """),
                {"now": now, "any_type": not job_types, "types": job_types or []},
            ).mappings().first()
            if row is None:
                return None
            conn.execute(
                text("""UPDATE jobs SET status='RUNNING', attempts = attempts + 1, locked_by=:w,
                        locked_at=:now, updated_at=:now WHERE job_id=:id"""),
                {"w": worker_id, "now": now, "id": row["job_id"]},
            )
        payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
        return Job(job_id=row["job_id"], job_type=row["job_type"], payload=payload,
                   attempts=int(row["attempts"]) + 1, priority=int(row["priority"]),
                   organization_id=row["organization_id"], created_at=row["created_at"])

    def complete(self, job_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE jobs SET status='DONE', locked_by=NULL, updated_at=:now WHERE job_id=:id"),
                         {"now": self.clock.now(), "id": job_id})

    def fail(self, job_id: str, error_code: str, retry_in_s: float | None) -> None:
        now = self.clock.now()
        with self.engine.begin() as conn:
            attempts, max_attempts = conn.execute(
                text("SELECT attempts, max_attempts FROM jobs WHERE job_id=:id FOR UPDATE"), {"id": job_id}
            ).one()
            if retry_in_s is not None and attempts < max_attempts:
                conn.execute(
                    text("""UPDATE jobs SET status='QUEUED', run_after=:ra, last_error_code=:e,
                            locked_by=NULL, updated_at=:now WHERE job_id=:id"""),
                    {"ra": now + timedelta(seconds=retry_in_s), "e": error_code, "now": now, "id": job_id},
                )
            else:
                conn.execute(
                    text("""UPDATE jobs SET status='FAILED', last_error_code=:e, locked_by=NULL,
                            updated_at=:now WHERE job_id=:id"""),
                    {"e": error_code, "now": now, "id": job_id},
                )

    def depth_by_type(self) -> dict[str, int]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT job_type, count(*) FROM jobs WHERE status='QUEUED' GROUP BY job_type")
            ).all()
        return {r[0]: int(r[1]) for r in rows}
