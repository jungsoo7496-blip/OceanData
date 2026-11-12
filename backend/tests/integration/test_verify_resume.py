"""04번 요구사항 4.2·4.4·4.6: 무결성 검증은 중간에 끊겨도 다시 실행하면 이어서 끝난다 (지시서 12번 독립 검토 — 업로드 F2·F3, ADR-0058).

검증은 두 트랜잭션이다: ① RECEIVED→VALIDATING을 확정한다 ② 객체를 다시 읽어 해시를 대조한 뒤 등록하고 예약을 정산한다.
①이 확정된 뒤 워커가 죽거나(임대 만료로 작업이 되돌아온다) 스트림 오류가 나면, 다시 실행한 검증이 'VALIDATING이라 이미 처리됨'으로
조용히 끝나 자산이 VALIDATING에 영구히 남고 업로드 예약이 풀리지 않았다(쿼터 영구 감소, 삭제·재처리 409).
시험 데이터는 합성 값이다.
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.db.models import Asset, IntegrityLogEntry, OrgQuota, Upload
from app.seed import DEV_A_SENSOR, ORG_A, SES_A
from app.services import ingest as ingest_mod
from app.services.ingest import IntegrityVerifier
from app.workers import main as worker
from tests.integration.test_ingest import drain, manifest, mp4_bytes, upload_all

pytestmark = pytest.mark.integration


class _SimulatedKill(BaseException):
    """Exception이 아니라서 작업 루프가 잡지 못한다 — SIGKILL·OOM으로 죽은 워커처럼."""


def _quota(ctx) -> dict[str, int]:
    with ctx.c.sessionmaker() as db:
        q = db.get(OrgQuota, ORG_A)
        return {"used": q.used_bytes, "reserved": q.reserved_bytes}


def _entries(ctx, asset_id: str) -> int:
    with ctx.c.sessionmaker() as db:
        return len(db.scalars(select(IntegrityLogEntry).where(IntegrityLogEntry.asset_id == asset_id)).all())


def _uploaded(ictx, seed: int) -> tuple[dict, str, int]:
    data = mp4_bytes(128, seed=seed)
    created = upload_all(ictx, manifest(data), data, ictx.as_user(102))
    return created, created["asset_id"], len(data)


def _assert_registered_once(ictx, created: dict, size: int) -> None:
    aid = created["asset_id"]
    with ictx.c.sessionmaker() as db:
        asset, up = db.get(Asset, aid), db.get(Upload, created["upload_id"])
        assert asset.ingest_status == "REGISTERED", asset.ingest_status
        assert (up.status, up.reserved_bytes) == ("RECEIVED", 0)
    assert _quota(ictx) == {"used": size, "reserved": 0}, "예약이 정산되지 않았거나 두 번 정산됐다"
    assert _entries(ictx, aid) == 1, "무결성 로그에 한 번만 올라야 한다"


def test_verification_resumes_after_the_worker_died_mid_hash(ictx, monkeypatch) -> None:
    created, aid, size = _uploaded(ictx, 981)
    job = ictx.c.queue.claim(["integrity_verify"], "w-dies")
    real = ictx.c.storage.iter_object

    def killed(*_a, **_k):
        raise _SimulatedKill()
        yield b""  # pragma: no cover

    monkeypatch.setattr(ictx.c.storage, "iter_object", killed)
    with pytest.raises(_SimulatedKill):
        IntegrityVerifier(ictx.c).verify(job.payload["asset_id"])  # ①은 확정됐고 ②에서 죽는다
    monkeypatch.setattr(ictx.c.storage, "iter_object", real)
    with ictx.c.sessionmaker() as db:
        assert db.get(Asset, aid).ingest_status == "VALIDATING"

    with ictx.c.engine.begin() as conn:  # 임대가 지나면 작업이 대기열로 돌아온다
        conn.execute(text("UPDATE jobs SET lease_expires_at = now() - interval '1 minute' WHERE status = 'RUNNING'"))
    assert ictx.c.queue.reap_expired_leases() == 1
    drain(ictx)

    _assert_registered_once(ictx, created, size)


def test_verification_retry_after_a_stream_error_registers_the_asset(ictx, monkeypatch) -> None:
    created, aid, size = _uploaded(ictx, 901)
    real = ictx.c.storage.iter_object

    def broken(*_a, **_k):
        raise TimeoutError("synthetic stream error")
        yield b""  # pragma: no cover

    monkeypatch.setattr(ictx.c.storage, "iter_object", broken)
    worker.run_once(ictx.c, "t")  # 첫 시도가 실패해 작업이 재시도 대기로 간다
    monkeypatch.setattr(ictx.c.storage, "iter_object", real)
    with ictx.c.engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET run_after = now() WHERE status = 'QUEUED'"))
    drain(ictx)

    _assert_registered_once(ictx, created, size)


def test_overlapping_verification_runs_register_once(ictx, monkeypatch) -> None:
    """임대가 만료돼 같은 작업이 겹쳐 돌아도 등록·정산은 한 번이다 (먼저 끝낸 쪽이 하고 늦은 쪽은 상태를 보고 물러난다)."""
    created, aid, size = _uploaded(ictx, 911)
    real = ictx.c.storage.iter_object
    first_is_streaming, release_first, result = threading.Event(), threading.Event(), {}

    def gated(*a, **k):
        if threading.current_thread().name == "first-run":
            first_is_streaming.set()
            assert release_first.wait(30)
        yield from real(*a, **k)

    monkeypatch.setattr(ictx.c.storage, "iter_object", gated)

    def first() -> None:
        try:
            result["first"] = IntegrityVerifier(ictx.c).verify(aid)
        except Exception as exc:  # noqa: BLE001
            result["first"] = f"{type(exc).__name__}: {exc}"

    t = threading.Thread(target=first, name="first-run")
    t.start()
    assert first_is_streaming.wait(30)  # ①을 확정하고 해시를 읽는 중이다
    assert IntegrityVerifier(ictx.c).verify(aid) == "REGISTERED"  # 겹친 두 번째 실행이 이어서 끝낸다
    release_first.set()
    t.join(30)

    assert result["first"] == "REGISTERED", result  # 늦은 쪽은 이미 끝난 상태를 보고 물러난다
    _assert_registered_once(ictx, created, size)


def test_revalidation_takes_the_integrity_lock_before_the_quota_row(ictx, monkeypatch) -> None:
    """다른 파생 작업(record_derived)은 무결성 로그 잠금 → 쿼터 행 순서다. 재검증이 반대로 잡으면 교착이 나고,
    교착 피해자가 되면 객체는 이미 옮겨졌는데 DB는 되돌아가 기록된 위치에 객체가 없다."""
    data = b"time,temp\n2026-11-01T00:00:00Z,7\n"
    m = manifest(data, source=DEV_A_SENSOR, data_type="SENSOR", variant="ORIGINAL", mime="text/csv", session=None,
                 timestamp_quality={"source": "UNKNOWN", "verified": False})
    m.pop("capture_start_at")
    h = ictx.as_user(102)
    aid = upload_all(ictx, m, data, h)["asset_id"]
    drain(ictx)  # 회차 미상이라 격리된다
    r = ictx.client.patch(f"/v1/assets/{aid}", json={"session_id": SES_A}, headers=h)
    assert r.status_code == 200 and r.json()["ingest_status"] == "VALIDATING", r.text

    seen: dict[str, bool] = {}
    real = ingest_mod.append_integrity

    def probe(db, asset, now):
        with ictx.c.engine.connect() as conn:  # 다른 연결에서 쿼터 행을 NOWAIT로 잠가 본다
            try:
                conn.execute(text(f"SELECT 1 FROM {OrgQuota.__tablename__} WHERE organization_id = :o FOR UPDATE NOWAIT"),
                             {"o": asset.organization_id})
                seen["quota_row_was_free"] = True
            except DBAPIError:
                seen["quota_row_was_free"] = False
            finally:
                conn.rollback()
        return real(db, asset, now)

    monkeypatch.setattr(ingest_mod, "append_integrity", probe)
    assert IntegrityVerifier(ictx.c).verify(aid) in ("REGISTERED", "READY_PRIVATE")

    assert seen == {"quota_row_was_free": True}, "무결성 로그 잠금을 잡을 때 이미 쿼터 행을 쥐고 있다 (잠금 순서 뒤집힘)"
