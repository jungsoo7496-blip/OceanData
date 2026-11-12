"""04번 격리 사용량 회귀 시험 (실제 S3 API, moto 또는 TEST_S3_ENDPOINT). 검토 2026-11-12 C11, ADR-0060.

격리 사용량(`quarantine_bytes`)은 격리 존에 있는 객체의 바이트다. 격리로 옮기지 못한 객체(저장소 오류)는 원래 존에 남으므로
사용량(`used_bytes`)으로 센다. 삭제·재검증이 존을 보고 빼므로, 같은 기준으로 세지 않으면 바이트가 새거나 두 번 센다.
"""

from __future__ import annotations

import pytest

from app.db.models import Asset
from app.seed import DEV_A_SENSOR, SES_A
from tests.integration.test_ingest import drain, manifest, mp4_bytes, quota, upload_all

pytestmark = pytest.mark.integration


def _failing_move(*_args: object, **_kwargs: object) -> None:
    raise TimeoutError("합성 저장소 장애: 격리 이동 실패")


def _asset(ictx, asset_id: str) -> Asset:  # type: ignore[no-untyped-def]
    with ictx.c.sessionmaker() as db:
        a = db.get(Asset, asset_id)
        assert a is not None
        return a


def test_quarantine_move_failure_then_delete_releases_all_bytes(ictx, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """격리 이동이 실패해 원래 존에 남은 자산을 삭제하면 사용량이 0으로 돌아온다 (격리 사용량이 새지 않는다)."""
    h = ictx.as_user(102)
    data = mp4_bytes(256, seed=971)
    created = upload_all(ictx, manifest(data, sha="0" * 64), data, h)  # 신고 체크섬이 틀려 CHECKSUM_MISMATCH
    with monkeypatch.context() as patch:
        patch.setattr(ictx.c.storage, "move_object", _failing_move)
        drain(ictx)
    a = _asset(ictx, created["asset_id"])
    assert (a.ingest_status, a.quarantine_reason, a.storage_zone) == ("QUARANTINED", "CHECKSUM_MISMATCH", "PROCESSED")
    q = quota(ictx)
    # 객체는 원래 존(PROCESSED)에 있다 — 격리 존이 아니므로 격리 사용량이 아니라 사용량으로 센다
    assert (q.quarantine_bytes, q.used_bytes, q.reserved_bytes) == (0, len(data), 0)

    r = ictx.client.post(f"/v1/assets/{created['asset_id']}/deletion", json={"reason": "손상 파일 폐기 (합성)"},
                         headers=ictx.as_user(101, mfa=True))
    assert r.is_success, r.text
    q = quota(ictx)
    assert (q.quarantine_bytes, q.used_bytes, q.reserved_bytes) == (0, 0, 0)


def test_revalidation_after_failed_quarantine_move_counts_bytes_once(ictx, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """격리 이동이 실패한 회차 미상 자산을 보완·재검증해도 바이트는 한 번만 센다."""
    h = ictx.as_user(102)
    data = b"time,temp\n2026-11-01T00:00:00Z,18.4\n"
    m = manifest(data, source=DEV_A_SENSOR, data_type="SENSOR", variant="ORIGINAL", mime="text/csv", session=None,
                 timestamp_quality={"source": "UNKNOWN", "verified": False})
    m.pop("capture_start_at")
    created = upload_all(ictx, m, data, h)
    with monkeypatch.context() as patch:
        patch.setattr(ictx.c.storage, "move_object", _failing_move)
        drain(ictx)
    a = _asset(ictx, created["asset_id"])
    assert (a.ingest_status, a.quarantine_reason, a.storage_zone) == ("QUARANTINED", "UNKNOWN_SESSION", "RAW")
    old_key = a.storage_key

    r = ictx.client.patch(f"/v1/assets/{created['asset_id']}", json={"session_id": SES_A}, headers=h)
    assert r.status_code == 200 and r.json()["ingest_status"] == "VALIDATING", r.text
    drain(ictx)
    a = _asset(ictx, created["asset_id"])
    assert (a.ingest_status, a.quarantine_reason) == ("READY_PRIVATE", None)
    assert a.storage_zone == "RAW" and f"/{SES_A}/" in (a.storage_key or "")  # 회차가 들어간 정식 위치로 옮겼다
    stored = ictx.c.storage.head_object(ictx.c.settings.buckets["RAW"], a.storage_key)
    assert stored is not None and stored.size == len(data)
    assert ictx.c.storage.head_object(ictx.c.settings.buckets["RAW"], old_key) is None
    q = quota(ictx)
    assert (q.quarantine_bytes, q.used_bytes, q.reserved_bytes) == (0, len(data), 0)


def test_requarantine_moves_bytes_when_object_finally_reaches_quarantine_zone(ictx, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """지난 격리 때 옮기지 못한 객체가 재검증에서 다시 격리되며 격리 존으로 옮겨지면 사용량에서 격리 사용량으로 옮겨 센다."""
    from app.services import ingest as ingest_mod

    h = ictx.as_user(102)
    data = b"time,temp\n2026-11-01T00:00:00Z,18.4\n"
    m = manifest(data, source=DEV_A_SENSOR, data_type="SENSOR", variant="ORIGINAL", mime="text/csv", session=None,
                 timestamp_quality={"source": "UNKNOWN", "verified": False})
    m.pop("capture_start_at")
    created = upload_all(ictx, m, data, h)
    with monkeypatch.context() as patch:
        patch.setattr(ictx.c.storage, "move_object", _failing_move)
        drain(ictx)
    q = quota(ictx)
    assert (_asset(ictx, created["asset_id"]).storage_zone, q.quarantine_bytes, q.used_bytes) == ("RAW", 0, len(data))

    r = ictx.client.patch(f"/v1/assets/{created['asset_id']}", json={"session_id": SES_A}, headers=h)
    assert r.status_code == 200, r.text
    with monkeypatch.context() as patch:
        patch.setattr(ingest_mod, "sniff", lambda _head: None)  # 재검증에서 다른 사유(UNSUPPORTED_FORMAT)로 다시 격리
        drain(ictx)
    a = _asset(ictx, created["asset_id"])
    assert (a.ingest_status, a.quarantine_reason, a.storage_zone) == ("QUARANTINED", "UNSUPPORTED_FORMAT", "QUARANTINE")
    q = quota(ictx)
    assert (q.quarantine_bytes, q.used_bytes, q.reserved_bytes) == (len(data), 0, 0)
