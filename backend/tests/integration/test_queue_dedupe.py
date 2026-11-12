"""작업 큐 dedupe_key — 활성(QUEUED·RUNNING) 동안만 막는 쪽과 끝난 뒤에도 막는 쪽.

계약 점검에서 찾은 문제: `POST /v1/backup-runs`와 정기 백업이 상수 키 "backup"으로 넣는데, 키의 유일 색인은 작업 상태와 무관하게
걸려 있어 첫 백업 작업이 DONE이 된 뒤에도 키를 쥐었다. 그래서 두 번째부터는 새 작업이 만들어지지 않고 옛 작업 ID만 돌려줬다.
값은 모두 합성이다.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db.models import JobRow
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration
OP = 106


def _finish(ctx, job_id: str, job_type: str = "noop") -> None:  # type: ignore[no-untyped-def]
    job = ctx.c.queue.claim([job_type], "w-dedupe")
    assert job is not None and job.job_id == job_id
    ctx.c.queue.complete(job_id)


def _row(ctx, job_id: str) -> JobRow:  # type: ignore[no-untyped-def]
    with ctx.c.sessionmaker() as db:
        row = db.get(JobRow, job_id)
        assert row is not None
        return row


def test_default_dedupe_holds_after_the_job_finished(ctx) -> None:
    """기본 동작은 그대로: 같은 키는 끝난 뒤에도 같은 작업을 돌려준다 (파이프라인 단계가 한 번만 돌게)."""
    first = ctx.c.queue.enqueue("noop", {}, dedupe_key="once-ever")
    _finish(ctx, first)
    assert ctx.c.queue.enqueue("noop", {}, dedupe_key="once-ever") == first


def test_active_only_dedupe_blocks_while_active_and_releases_after_done_or_failed(ctx) -> None:
    q = ctx.c.queue
    first = q.enqueue("noop", {}, dedupe_key="k-active", dedupe_active_only=True)
    assert q.enqueue("noop", {}, dedupe_key="k-active", dedupe_active_only=True) == first  # 대기 중이면 같은 작업
    job = q.claim(["noop"], "w-dedupe")
    assert job is not None and job.job_id == first
    assert q.enqueue("noop", {}, dedupe_key="k-active", dedupe_active_only=True) == first  # 실행 중에도

    q.complete(first)
    second = q.enqueue("noop", {}, dedupe_key="k-active", dedupe_active_only=True)
    assert second != first  # 끝났으니 새 작업
    assert _row(ctx, first).status == "DONE" and _row(ctx, first).dedupe_key != "k-active"  # 기록은 남고 키만 놓는다
    assert _row(ctx, second).status == "QUEUED" and _row(ctx, second).dedupe_key == "k-active"

    # 실패로 끝난 작업도 키를 쥐지 않는다
    with ctx.c.engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET status = 'FAILED' WHERE job_id = :id"), {"id": second})
    third = q.enqueue("noop", {}, dedupe_key="k-active", dedupe_active_only=True)
    assert third not in (first, second) and _row(ctx, third).dedupe_key == "k-active"


def test_active_only_flag_does_not_release_a_key_held_by_an_ordinary_job_that_is_still_active(ctx) -> None:
    q = ctx.c.queue
    held = q.enqueue("noop", {}, dedupe_key="k-shared")  # 기본(끝난 뒤에도 막는) 작업, 아직 대기 중
    assert q.enqueue("noop", {}, dedupe_key="k-shared", dedupe_active_only=True) == held


def test_backup_runs_api_queues_a_new_job_after_the_previous_one_finished(ctx) -> None:
    op = ctx.as_user(OP)
    r1 = ctx.client.post("/v1/backup-runs", headers=op)
    check(r1, "POST", "/v1/backup-runs")
    assert r1.status_code == 202, r1.text
    first = r1.json()["job_id"]
    assert ctx.client.post("/v1/backup-runs", headers=op).json()["job_id"] == first  # 대기 중이면 같은 작업

    _finish(ctx, first, "backup")
    r3 = ctx.client.post("/v1/backup-runs", headers=op)
    assert r3.status_code == 202 and r3.json()["job_id"] != first  # 예전에는 계속 first가 돌아왔다
    assert [a for a in ctx.audit_actions() if a == "BACKUP_REQUESTED"] == ["BACKUP_REQUESTED"] * 3
