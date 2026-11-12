"""04번 요구사항 4.1·4.7·1.5: RECEIVED에 멈춘 자산의 검증 재투입 (검토 2026-11-12 C2 남은 부분, ADR-0071).

기관 정책이 악성코드 검사를 요구하는데 검사기가 없으면 무결성 검증 작업이 5회 실패(약 4분)하고 FAILED로 끝난다.
`RECEIVED → FAILED`는 domain.md 5장 표에 없어 자산은 `RECEIVED`에 남고 업로드 예약도 그대로다. 같은 dedupe 키의 작업이
끝난 뒤에도 키를 쥐어 새 작업을 넣을 길이 없었고, 복구는 DB 직접 조작뿐이었다. 운영자 명령 `requeue-verify`가
대기·실행 중인 검증 작업이 없는 RECEIVED 자산을 찾아(--dry-run) 검증 작업을 다시 넣는다. 앞 작업의 끝 처리가 늦게 돌아도
새 작업의 검증을 FAILED로 만들지 않는다. 시험 데이터는 합성 값이다.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import select, text

from app import cli
from app.db.models import Asset, AuditLog, IntegrityLogEntry, JobRow, Organization, OrgQuota, Upload
from app.seed import DEV_A_SENSOR, ORG_A, ORG_B
from app.workers import main as worker
from tests.conftest import Ctx
from tests.integration.test_ingest import drain, manifest, mp4_bytes, upload_all

pytestmark = pytest.mark.integration
MEMBER_A = 102
ACTION = "INTEGRITY_VERIFY_REQUEUED"
REASON = "검사기 복구 뒤 재투입 (시험)"

Run = Callable[..., tuple[int, Any, str]]


@pytest.fixture
def holder(ictx: Ctx) -> dict[str, Any]:
    """명령이 쓸 컨테이너. 검사기가 '돌아온' 뒤에는 시험이 바꿔 끼운다 (운영에서는 MALWARE_SCANNER를 바꾸고 다시 띄운다)."""
    return {"c": ictx.c}


@pytest.fixture
def run_cli(ictx: Ctx, holder: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
            capsys: pytest.CaptureFixture[str]) -> Iterator[Run]:
    settings = ictx.c.settings
    for key in ("database_url", "s3_endpoint_url", "s3_access_key_id", "s3_secret_access_key",
                "idp_issuer_url", "device_token_signing_key"):
        value = getattr(settings, key)
        monkeypatch.setenv(key.upper(), value.get_secret_value() if isinstance(value, SecretStr) else value)
    monkeypatch.setattr(cli, "build_container", lambda settings: holder["c"])
    loggers = [logging.getLogger(name) for name in ("", "uvicorn.access", "uvicorn.error")]
    originals = [(list(logger.handlers), logger.level, logger.propagate) for logger in loggers]
    cli.get_settings.cache_clear()

    def run(*args: str) -> tuple[int, Any, str]:
        capsys.readouterr()  # 앞선 워커 로그를 버린다
        code = cli.main(["requeue-verify", *args])
        out = capsys.readouterr()
        return code, json.loads(out.out) if out.out.strip() else None, out.err

    try:
        yield run
    finally:
        cli.get_settings.cache_clear()
        for logger, (handlers, level, propagate) in zip(loggers, originals, strict=True):
            logger.handlers[:] = handlers
            logger.setLevel(level)
            logger.propagate = propagate


def _csv(n: int) -> bytes:  # 같은 내용은 중복 등록이라 값을 바꾼다
    return f"observed_at,value\n2026-11-01T00:00:00Z,{n}\n2026-11-01T00:10:00Z,{n + 1}\n".encode()


def _upload(ictx: Ctx, n: int) -> tuple[str, str, int]:
    data = _csv(n)
    m = manifest(data, source=DEV_A_SENSOR, data_type="SENSOR", variant="ORIGINAL", mime="text/csv")
    created = upload_all(ictx, m, data, ictx.as_user(MEMBER_A))
    return created["asset_id"], created["upload_id"], len(data)


def _scan_required(ictx: Ctx, on: bool) -> None:
    with ictx.c.sessionmaker.begin() as db:
        org = db.get(Organization, ORG_A)
        assert org is not None
        org.malware_scan_required = on


def _with_scanner(ictx: Ctx) -> Any:
    return dataclasses.replace(ictx.c, settings=ictx.c.settings.model_copy(update={"malware_scanner": "mock-eicar"}))


def _quota(ictx: Ctx) -> dict[str, int]:
    with ictx.c.sessionmaker() as db:
        q = db.get(OrgQuota, ORG_A)
        return {"used": q.used_bytes if q else 0, "reserved": q.reserved_bytes if q else 0}


def _verify_jobs(ictx: Ctx, aid: str) -> list[tuple[str, str, int]]:
    with ictx.c.sessionmaker() as db:
        rows = db.scalars(select(JobRow).where(JobRow.job_type == "integrity_verify", JobRow.asset_id == aid)
                          .order_by(JobRow.created_at, JobRow.job_id)).all()
        return [(j.job_id, j.status, j.attempts) for j in rows]


def _exhaust(c: Any, aid: str) -> None:
    """검사기 없이 검증 작업을 최대 시도까지 돌린다 (재시도 대기는 시각을 당겨 건너뛴다)."""
    for _ in range(5):
        with c.engine.begin() as conn:
            conn.execute(text("UPDATE jobs SET run_after = now() WHERE status = 'QUEUED' AND asset_id = :a"),
                         {"a": aid})
        assert worker.run_once(c, "t-exhaust")


def _audits(ictx: Ctx) -> list[AuditLog]:
    with ictx.c.sessionmaker() as db:
        return list(db.scalars(select(AuditLog).where(AuditLog.action == ACTION).order_by(AuditLog.seq)).all())


def test_stuck_received_asset_is_listed_requeued_and_registered_once(ictx: Ctx, holder: dict[str, Any],
                                                                      run_cli: Run) -> None:
    _scan_required(ictx, True)
    base = _quota(ictx)
    aid, upload_id, size = _upload(ictx, 7101)

    # 검사기 없음 → 5회 실패 → 작업 FAILED, 자산은 RECEIVED에 남고 예약도 그대로 (RECEIVED → FAILED는 표에 없다)
    _exhaust(ictx.c, aid)
    [(first_job, status, attempts)] = _verify_jobs(ictx, aid)
    assert (status, attempts) == ("FAILED", 5)
    with ictx.c.sessionmaker() as db:
        asset, up = db.get(Asset, aid), db.get(Upload, upload_id)
        assert asset.ingest_status == "RECEIVED"
        assert "failed_from" not in (asset.ingest_context or {})  # 끝 처리(mark_failed)는 아무것도 바꾸지 않는다
        assert up.reserved_bytes == size
    assert _quota(ictx) == {"used": base["used"], "reserved": base["reserved"] + size}

    # 운영자 확인: dry-run이 멈춘 자산을 보여 주고 아무것도 바꾸지 않는다
    code, out, err = run_cli("--all-stuck", "--dry-run")
    assert code == 0, err
    assert out["dry_run"] is True and out["malware_scanner"] == "none"
    [row] = out["stuck"]
    assert row["asset_id"] == aid and row["organization_id"] == ORG_A
    assert row["reserved_bytes"] == size and row["malware_scan_required"] is True
    assert row["last_job"]["job_id"] == first_job
    assert (row["last_job"]["status"], row["last_job"]["attempts"], row["last_job"]["last_error_code"]) == \
        ("FAILED", 5, "INTERNAL")
    assert len(_verify_jobs(ictx, aid)) == 1 and _audits(ictx) == []

    # 사유 없이는 실행하지 않는다
    code, _, err = run_cli("--all-stuck")
    assert code == 1 and "--reason" in err
    assert len(_verify_jobs(ictx, aid)) == 1

    # 검사기가 아직 없는데 다시 넣으면: 새 작업이 생기고(끝난 작업의 키가 막지 않는다) 경고가 나온다
    code, out, err = run_cli("--all-stuck", "--reason", REASON)
    assert code == 0, err
    [requeued] = out["requeued"]
    second_job = requeued["job_id"]
    assert requeued["asset_id"] == aid and second_job != first_job
    assert "MALWARE_SCANNER" in err
    # 두 번 돌려도 일을 더 만들지 않는다 (대기 중인 작업이 있다)
    code, out, err = run_cli("--all-stuck", "--reason", REASON)
    assert code == 0, err
    assert out["requeued"] == [] and out["skipped"] == []  # --all-stuck은 멈춘 자산만 고른다
    code, out, err = run_cli("--asset", aid, "--reason", REASON)
    assert code == 0, err
    assert out["requeued"] == []
    assert out["skipped"] == [{"asset_id": aid, "reason": "VERIFY_JOB_ACTIVE",
                               "job": {**out["skipped"][0]["job"], "job_id": second_job, "status": "QUEUED"}}]
    assert [s for _, s, _ in _verify_jobs(ictx, aid)] == ["FAILED", "QUEUED"]

    _exhaust(ictx.c, aid)  # 또 실패해도 자산·예약은 그대로
    assert [s for _, s, _ in _verify_jobs(ictx, aid)] == ["FAILED", "FAILED"]
    assert _quota(ictx) == {"used": base["used"], "reserved": base["reserved"] + size}

    # 검사기가 돌아온 뒤: 다시 넣고 → 워커가 검증 → 등록, 예약은 한 번만 정산
    scanning = _with_scanner(ictx)
    holder["c"] = scanning
    code, out, err = run_cli("--all-stuck", "--reason", REASON)
    assert code == 0, err
    assert out["malware_scanner"] == "mock-eicar" and "MALWARE_SCANNER" not in err
    [requeued] = out["requeued"]
    third_job = requeued["job_id"]
    assert third_job not in (first_job, second_job)
    assert requeued["previous_job"]["job_id"] == second_job
    assert worker.run_once(scanning, "t-scan")
    with ictx.c.sessionmaker() as db:
        asset, up = db.get(Asset, aid), db.get(Upload, upload_id)
        assert asset.ingest_status == "READY_PRIVATE", asset.ingest_status
        assert asset.ingest_context["malware_scan"]["result"] == "CLEAN"
        assert (up.status, up.reserved_bytes) == ("RECEIVED", 0)
        assert len(db.scalars(select(IntegrityLogEntry).where(IntegrityLogEntry.asset_id == aid)).all()) == 1
    assert _quota(ictx) == {"used": base["used"] + size, "reserved": base["reserved"]}
    assert [s for _, s, _ in _verify_jobs(ictx, aid)] == ["FAILED", "FAILED", "DONE"]

    # 등록된 뒤에는 대상이 아니다
    code, out, err = run_cli("--all-stuck", "--reason", REASON)
    assert code == 0 and out["requeued"] == [], err
    code, out, err = run_cli("--asset", aid, "--reason", REASON)
    assert code == 0, err
    assert out["skipped"] == [{"asset_id": aid, "reason": "NOT_RECEIVED", "ingest_status": "READY_PRIVATE"}]
    assert len(_verify_jobs(ictx, aid)) == 3
    assert _quota(ictx) == {"used": base["used"] + size, "reserved": base["reserved"]}

    # 감사: 실제로 넣은 두 번만, 운영자 명령(SYSTEM/cli)·사유·새 작업·앞 작업
    audits = _audits(ictx)
    assert [(a.actor_kind, a.actor_id, a.organization_id, a.target_type, a.target_id, a.reason) for a in audits] == \
        [("SYSTEM", "cli", ORG_A, "Asset", aid, REASON)] * 2
    assert [a.details["job_id"] for a in audits] == [second_job, third_job]
    assert [a.details["previous_job"]["job_id"] for a in audits] == [first_job, second_job]
    assert audits[0].details["previous_job"]["status"] == "FAILED"
    assert audits[0].details["reserved_bytes"] == size
    _scan_required(ictx, False)


def _snapshot(ictx: Ctx) -> tuple[dict, dict]:
    with ictx.c.sessionmaker() as db:
        jobs = {j.job_id: (j.status, j.attempts, j.run_after, j.updated_at, j.dedupe_key, j.locked_by)
                for j in db.scalars(select(JobRow)).all()}
        assets = {a.asset_id: (a.ingest_status, a.updated_at, a.ingest_context, a.storage_zone, a.storage_key)
                  for a in db.scalars(select(Asset)).all()}
    return jobs, assets


def test_assets_with_active_jobs_or_other_states_are_left_alone(ictx: Ctx, run_cli: Run) -> None:
    done, _, _ = _upload(ictx, 7201)
    drain(ictx)  # 정상 처리된 자산
    running, _, _ = _upload(ictx, 7202)
    claimed = ictx.c.queue.claim(["integrity_verify"], "t-busy")  # 워커가 처리 중 (RUNNING)
    assert claimed is not None and claimed.payload["asset_id"] == running
    finished, _, _ = _upload(ictx, 7203)  # 검증 없이 작업만 DONE (같은 키 때문에 다시 넣을 수 없던 경우)
    stale = ictx.c.queue.claim(["integrity_verify"], "t-stale")
    assert stale is not None and stale.payload["asset_id"] == finished
    ictx.c.queue.complete(stale.job_id)
    missing, _, _ = _upload(ictx, 7204)  # 검증 작업이 아예 없다
    with ictx.c.engine.begin() as conn:
        conn.execute(text("DELETE FROM jobs WHERE asset_id = :a"), {"a": missing})
    queued, _, _ = _upload(ictx, 7205)  # 막 받은 자산 (QUEUED)
    with ictx.c.engine.begin() as conn:  # 재시도 대기처럼 실행 시각이 미래인 대기 작업도 진행 중이다
        conn.execute(text("UPDATE jobs SET run_after = now() + interval '1 hour', attempts = 2 WHERE asset_id = :a"),
                     {"a": queued})

    code, out, err = run_cli("--all-stuck", "--dry-run")
    assert code == 0, err
    assert sorted(r["asset_id"] for r in out["stuck"]) == sorted([finished, missing])
    assert {r["asset_id"]: r["last_job"] for r in out["stuck"]}[missing] is None
    code, out, err = run_cli("--all-stuck", "--dry-run", "--org", ORG_B)
    assert code == 0 and out["stuck"] == [], err

    before_jobs, before_assets = _snapshot(ictx)
    code, out, err = run_cli("--all-stuck", "--reason", REASON)
    assert code == 0, err
    assert sorted(r["asset_id"] for r in out["requeued"]) == sorted([finished, missing])
    after_jobs, after_assets = _snapshot(ictx)
    new_jobs = set(after_jobs) - set(before_jobs)
    assert {r["job_id"] for r in out["requeued"]} == new_jobs
    assert {jid: v for jid, v in after_jobs.items() if jid in before_jobs} == before_jobs  # 기존 작업은 그대로
    assert after_assets == before_assets  # 자산 행은 아무것도 바뀌지 않는다
    assert sorted(a.target_id for a in _audits(ictx)) == sorted([finished, missing])

    # 이름으로 고른 자산도 진행 중이거나 다른 상태면 건드리지 않는다
    for aid, why in ((running, "VERIFY_JOB_ACTIVE"), (queued, "VERIFY_JOB_ACTIVE"), (done, "NOT_RECEIVED")):
        code, out, err = run_cli("--asset", aid, "--reason", REASON)
        assert code == 0, err
        assert out["requeued"] == [] and [(s["asset_id"], s["reason"]) for s in out["skipped"]] == [(aid, why)]
    code, out, err = run_cli("--asset", running, "--dry-run")
    assert code == 0 and out["stuck"] == [], err
    assert [(s["asset_id"], s["reason"]) for s in out["skipped"]] == [(running, "VERIFY_JOB_ACTIVE")]
    # 없는 자산, 다른 기관으로 한정한 자산은 실패로 알린다
    assert run_cli("--asset", "ast_0000000000000000000000ZZZZ", "--reason", REASON)[0] == 1
    assert run_cli("--asset", finished, "--org", ORG_B, "--reason", REASON)[0] == 1
    assert _snapshot(ictx) == (after_jobs, after_assets)
    assert len(_audits(ictx)) == 2


def test_concurrent_requeues_make_one_job(ictx: Ctx, monkeypatch: pytest.MonkeyPatch, run_cli: Run) -> None:
    """두 운영자(또는 같은 명령 두 번)가 동시에 돌려도 작업·감사는 하나다. 앞선 실행이 자산 행을 쥔 동안 뒤 실행(명령)은
    기다리지 않고 건너뛴다."""
    from app.services import verify_requeue  # 수정 전에는 없는 모듈이라 다른 시험이 모이도록 여기서 불러온다

    aid, _, _ = _upload(ictx, 7301)
    with ictx.c.engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET status = 'FAILED', attempts = 5 WHERE asset_id = :a"), {"a": aid})
    real = ictx.c.queue.enqueue
    holding, release, result = threading.Event(), threading.Event(), {}

    def gated(*a: Any, **k: Any) -> str:
        if threading.current_thread().name == "first-requeue":
            holding.set()
            assert release.wait(30)
        return real(*a, **k)

    monkeypatch.setattr(ictx.c.queue, "enqueue", gated)

    def first() -> None:
        result["first"] = verify_requeue.requeue(ictx.c, [aid], reason=REASON)

    t = threading.Thread(target=first, name="first-requeue")
    t.start()
    assert holding.wait(30)  # 앞선 실행이 자산 행을 잠그고 작업을 넣는 중이다
    try:
        # 잠그지 않고 고른 후보에는 들어가지만(앞 실행의 작업은 아직 확정 전), 잠글 때 건너뛴다
        code, second, err = run_cli("--all-stuck", "--reason", REASON)
    finally:
        release.set()
        t.join(30)

    assert code == 0, err
    assert second["requeued"] == [] and second["failed"] == []
    assert second["skipped"] == [{"asset_id": aid, "reason": "LOCKED"}]
    assert [r["asset_id"] for r in result["first"].requeued] == [aid]
    code, third, err = run_cli("--asset", aid, "--reason", REASON)
    assert code == 0 and third["requeued"] == [], err
    assert [s["reason"] for s in third["skipped"]] == ["VERIFY_JOB_ACTIVE"]
    assert verify_requeue.requeue(ictx.c, [aid], reason=REASON).requeued == []  # 이름으로 바로 넣어도 잠근 뒤 다시 본다
    assert [s for _, s, _ in _verify_jobs(ictx, aid)] == ["FAILED", "QUEUED"]
    assert len(_audits(ictx)) == 1


def _status(ictx: Ctx, aid: str) -> str:
    with ictx.c.sessionmaker() as db:
        a = db.get(Asset, aid)
        assert a is not None
        return a.ingest_status


@pytest.mark.parametrize("late", ["while_new_job_reads", "after_new_job_registered"])
def test_old_jobs_late_exhaustion_does_not_fail_the_requeued_verification(
        ictx: Ctx, monkeypatch: pytest.MonkeyPatch, late: str) -> None:
    """큐는 앞 작업의 FAILED를 먼저 확정하고 끝 처리(on_exhausted → mark_failed)를 나중에 부른다. 그 틈에 운영자가 다시 넣고
    검사기가 있는 워커가 그 작업으로 RECEIVED → VALIDATING(또는 등록)까지 확정하면, 늦게 도는 앞 작업의 끝 처리가 자산을
    FAILED로 만들어서는 안 된다 — 그러면 예약은 묶인 채로 남고 재투입 대상(RECEIVED)에서도 빠진다."""
    from app.services import verify_requeue

    _scan_required(ictx, True)
    base = _quota(ictx)
    if late == "after_new_job_registered":  # 영상은 등록 뒤 REGISTERED에 머물고, REGISTERED → FAILED는 표에 있다
        data = mp4_bytes(64 * 1024, seed=7402)
        created = upload_all(ictx, manifest(data), data, ictx.as_user(MEMBER_A))
        aid, upload_id, size, registered = created["asset_id"], created["upload_id"], len(data), "REGISTERED"
    else:
        aid, upload_id, size = _upload(ictx, 7401)
        registered = "READY_PRIVATE"
    scanning = _with_scanner(ictx)
    exhausted = worker.HANDLERS["integrity_verify"]
    assert exhausted.on_exhausted is not None
    seen: dict[str, Any] = {}

    def late_exhaust(c: Any, job: Any, code: str) -> None:
        assert exhausted.on_exhausted is not None
        seen["requeued"] = [r["job_id"] for r in verify_requeue.requeue(scanning, [aid], reason=REASON).requeued]
        if late == "after_new_job_registered":
            assert worker.run_once(scanning, "t-new")
            seen["when_old_ends"] = _status(ictx, aid)
            exhausted.on_exhausted(c, job, code)
            return
        reads = ictx.c.storage.iter_object

        def iter_object(bucket: str, key: str) -> Iterator[bytes]:
            # 새 작업은 RECEIVED → VALIDATING을 확정하고 객체를 읽기 시작했다. 이때 앞 워커가 끝 처리를 한다
            seen["when_old_ends"] = _status(ictx, aid)
            assert exhausted.on_exhausted is not None
            exhausted.on_exhausted(c, job, code)
            yield from reads(bucket, key)

        monkeypatch.setattr(ictx.c.storage, "iter_object", iter_object)
        try:
            assert worker.run_once(scanning, "t-new")
        finally:
            monkeypatch.setattr(ictx.c.storage, "iter_object", reads)

    monkeypatch.setitem(worker.HANDLERS, "integrity_verify", worker.Handler(exhausted.fn, late_exhaust))
    _exhaust(ictx.c, aid)  # 다섯 번째 실패에서 late_exhaust가 돈다

    assert seen["when_old_ends"] == ("VALIDATING" if late == "while_new_job_reads" else registered)
    jobs = _verify_jobs(ictx, aid)
    assert [(s, n) for _, s, n in jobs] == [("FAILED", 5), ("DONE", 1)]
    assert seen["requeued"] == [jobs[1][0]]
    with ictx.c.sessionmaker() as db:
        asset, up = db.get(Asset, aid), db.get(Upload, upload_id)
        assert asset.ingest_status == registered, (asset.ingest_status, asset.ingest_context)
        assert "failed_from" not in (asset.ingest_context or {})
        assert asset.ingest_context["malware_scan"]["result"] == "CLEAN"
        assert up.reserved_bytes == 0
        assert len(db.scalars(select(IntegrityLogEntry).where(IntegrityLogEntry.asset_id == aid)).all()) == 1
    assert _quota(ictx) == {"used": base["used"] + size, "reserved": base["reserved"]}
    _scan_required(ictx, False)


def test_storage_read_failure_ends_failed_from_validating_and_is_not_listed(
        ictx: Ctx, monkeypatch: pytest.MonkeyPatch, run_cli: Run) -> None:
    """객체를 읽지 못해 재시도를 다 쓰면 자산은 RECEIVED에 남지 않는다: 검증의 첫 트랜잭션이 RECEIVED → VALIDATING을
    확정한 뒤 읽으므로, 끝 처리가 VALIDATING → FAILED로 만든다(더 새 검증 작업이 없으면 끝 처리는 그대로 돈다).
    이 명령의 대상이 아니고, 예약이 풀리지 않는 것은 따로 볼 빈틈이다 (ADR-0071 감수할 점)."""
    aid, upload_id, size = _upload(ictx, 7501)

    def unreadable(bucket: str, key: str) -> Iterator[bytes]:
        raise OSError("저장소를 읽지 못함 (시험)")

    monkeypatch.setattr(ictx.c.storage, "iter_object", unreadable)
    _exhaust(ictx.c, aid)
    assert [(s, n) for _, s, n in _verify_jobs(ictx, aid)] == [("FAILED", 5)]
    with ictx.c.sessionmaker() as db:
        asset, up = db.get(Asset, aid), db.get(Upload, upload_id)
        assert asset.ingest_status == "FAILED"
        assert (asset.ingest_context["failed_from"], asset.ingest_context["error_code"]) == ("VALIDATING", "INTERNAL")
        assert up.reserved_bytes == size  # 지금 동작: FAILED에서도 예약이 묶여 있다 (이 변경 밖)

    code, out, err = run_cli("--all-stuck", "--dry-run")
    assert code == 0 and out["stuck"] == [], err
    code, out, err = run_cli("--asset", aid, "--dry-run")
    assert code == 0 and out["stuck"] == [], err
    assert out["skipped"] == [{"asset_id": aid, "reason": "NOT_RECEIVED", "ingest_status": "FAILED"}]


def test_one_asset_failing_is_reported_and_the_rest_are_requeued(ictx: Ctx, monkeypatch: pytest.MonkeyPatch,
                                                                  run_cli: Run) -> None:
    """한 자산의 재투입이 실패해도 다른 자산은 넣는다. 실패한 자산은 작업·감사 없이 `failed`에 남고 종료 코드는 1이다.
    표준 출력은 결과 JSON 하나뿐이어야 한다(운영자가 그대로 읽는다) — 실패 안내는 표준 오류로 간다."""
    broken, _, _ = _upload(ictx, 7601)
    fine, _, _ = _upload(ictx, 7602)
    with ictx.c.engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET status = 'FAILED', attempts = 5 WHERE asset_id IN (:a, :b)"),
                     {"a": broken, "b": fine})
    real = ictx.c.queue.enqueue

    def flaky(job_type: str, payload: dict[str, Any], **kw: Any) -> str:
        if payload.get("asset_id") == broken:
            raise RuntimeError("큐에 넣지 못함 (시험)")
        return real(job_type, payload, **kw)

    monkeypatch.setattr(ictx.c.queue, "enqueue", flaky)
    code, out, err = run_cli("--all-stuck", "--reason", REASON)  # 표준 출력이 JSON 하나가 아니면 여기서 깨진다
    assert code == 1
    assert out["failed"] == [{"asset_id": broken, "error_type": "RuntimeError"}]
    assert [r["asset_id"] for r in out["requeued"]] == [fine] and out["skipped"] == []
    assert "1건" in err and "failed" in err
    assert [a.target_id for a in _audits(ictx)] == [fine]
    assert [s for _, s, _ in _verify_jobs(ictx, broken)] == ["FAILED"]  # 작업도 감사도 함께 취소됐다

    monkeypatch.setattr(ictx.c.queue, "enqueue", real)  # 원인이 없어진 뒤 다시 돌리면 남은 자산만 넣는다
    code, out, err = run_cli("--all-stuck", "--reason", REASON)
    assert code == 0, err
    assert [r["asset_id"] for r in out["requeued"]] == [broken] and out["failed"] == []
    assert sorted(a.target_id for a in _audits(ictx)) == sorted([broken, fine])
