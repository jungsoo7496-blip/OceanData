"""03번 활성 품질 규칙과 운영자 재평가 명령의 PostgreSQL 통합 시험."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app import cli
from app.db.models import Asset, AuditLog, Device, QualityRuleSet
from app.seed import DEV_A_CAMERA, DEV_A_SENSOR, DEV_B_CAMERA, ORG_A, ORG_B, PRJ_A, PRJ_B, SES_B
from app.services.ingest import IntegrityVerifier
from app.services.quality import QualityRuleError
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_catalog import T0, _asset
from tests.integration.test_ingest import manifest, mp4_bytes, upload_all

pytestmark = pytest.mark.integration
COMMAND = "reevaluate-quality"
ACTION = "ASSET_QUALITY_REEVALUATED"


@pytest.fixture
def cli_container(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    settings = ctx.c.settings
    for key in ("database_url", "s3_endpoint_url", "s3_access_key_id", "s3_secret_access_key",
                "idp_issuer_url", "device_token_signing_key"):
        value = getattr(settings, key)
        monkeypatch.setenv(key.upper(), value.get_secret_value() if isinstance(value, SecretStr) else value)
    monkeypatch.setattr(cli, "build_container", lambda settings: ctx.c)
    loggers = [logging.getLogger(name) for name in ("", "uvicorn.access", "uvicorn.error")]
    originals = [(list(logger.handlers), logger.level, logger.propagate) for logger in loggers]
    cli.get_settings.cache_clear()
    try:
        yield
    finally:
        cli.get_settings.cache_clear()
        for logger, (handlers, level, propagate) in zip(loggers, originals, strict=True):
            logger.handlers[:] = handlers
            logger.setLevel(level)
            logger.propagate = propagate


def _rule(version: int, *, active: bool = True, **rules: Any) -> QualityRuleSet:
    return QualityRuleSet(version=version, rules={"version": version, **rules}, active=active,
                          created_by="test", created_at=T0)


def _result(capsys: pytest.CaptureFixture[str], *args: str) -> dict[str, int]:
    assert cli.main([COMMAND, ORG_A, *args]) == 0
    return json.loads(capsys.readouterr().out)


def _snapshot(asset: Asset) -> tuple:
    return (asset.quality_status, asset.quality_reasons, asset.ingest_context, asset.updated_at,
            asset.ingest_status, asset.quarantine_reason, asset.storage_zone, asset.storage_key, asset.deleted_at)


@pytest.mark.usefixtures("cli_container")
def test_cli_uses_default_and_active_rules_and_repeats_idempotently(ctx: Ctx, capsys: pytest.CaptureFixture[str]) -> None:
    video = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
    sensor = _asset(ORG_A, PRJ_A, DEV_A_SENSOR)
    sensor.data_type, sensor.mime_type = "SENSOR", ""
    sensor.timestamp_quality, sensor.capture_start_at = {"source": "DEVICE", "verified": True}, T0
    with ctx.c.sessionmaker.begin() as db:
        db.add_all([video, sensor])

    assert _result(capsys, "--asset-id", video.asset_id) == {
        "rule_version": 1, "evaluated": 1, "changed": 1, "skipped_missing_facts": 0,
    }
    with ctx.c.sessionmaker.begin() as db:
        current = db.get(Asset, video.asset_id)
        assert (current.quality_status, current.quality_reasons) == ("WARNING", ["capture_time_unknown"])
        assert db.get(Asset, sensor.asset_id).quality_status == "PENDING"
        db.get(QualityRuleSet, 1).active = False
        db.add_all([_rule(2, required_fields={"VIDEO": ["capture_start_at"]}), _rule(3, active=False)])

    result = {"rule_version": 2, "evaluated": 2, "changed": 2, "skipped_missing_facts": 0}
    assert _result(capsys) == result
    with ctx.c.sessionmaker() as db:
        evaluated_video = db.get(Asset, video.asset_id)
        evaluated_sensor = db.get(Asset, sensor.asset_id)
        assert (evaluated_video.quality_status, evaluated_video.quality_reasons) == (
            "FAILED", ["capture_time_unknown", "missing:capture_start_at"],
        )
        assert (evaluated_sensor.quality_status, evaluated_sensor.quality_reasons) == ("FAILED", ["missing:mime_type"])
        evaluated = (_snapshot(evaluated_video), _snapshot(evaluated_sensor))

    assert _result(capsys) == {**result, "changed": 0}
    with ctx.c.sessionmaker() as db:
        assert (_snapshot(db.get(Asset, video.asset_id)), _snapshot(db.get(Asset, sensor.asset_id))) == evaluated
        audit = db.scalars(select(AuditLog).where(AuditLog.action == ACTION).order_by(AuditLog.seq)).all()
        assert [(entry.actor_kind, entry.organization_id, entry.result) for entry in audit] == [
            ("SYSTEM", ORG_A, "SUCCESS"), ("SYSTEM", ORG_A, "SUCCESS"), ("SYSTEM", ORG_A, "SUCCESS"),
        ]
    assert ctx.c.audit.verify_chain().ok


@pytest.mark.usefixtures("cli_container")
def test_cli_keeps_protected_assets_and_missing_facts_unchanged(ctx: Ctx, capsys: pytest.CaptureFixture[str]) -> None:
    ready = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
    corrupt = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
    corrupt.ingest_status, corrupt.quarantine_reason = "QUARANTINED", "CORRUPT_FILE"
    corrupt.quality_status, corrupt.quality_reasons = "FAILED", ["not_playable"]
    corrupt.storage_zone, corrupt.storage_key = "QUARANTINE", "synthetic/corrupt.mp4"
    receiving = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
    receiving.ingest_status = "RECEIVED"
    deleted = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
    deleted.deleted_at = T0
    foreign = _asset(ORG_B, PRJ_B, DEV_B_CAMERA, session=SES_B)
    range_unknown = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
    range_unknown.quality_status, range_unknown.quality_reasons = "WARNING", ["out_of_range:temperature"]
    legacy = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
    legacy.timestamp_quality, legacy.capture_start_at = {"source": "DEVICE", "verified": True}, T0
    legacy.quality_status, legacy.quality_reasons = "FAILED", ["not_playable", "unknown_unit"]
    protected = [asset.asset_id for asset in (corrupt, receiving, deleted, foreign, range_unknown)]
    with ctx.c.sessionmaker.begin() as db:
        db.add_all([ready, corrupt, receiving, deleted, foreign, range_unknown, legacy])
    with ctx.c.sessionmaker() as db:
        before = {asset_id: _snapshot(db.get(Asset, asset_id)) for asset_id in protected}

    assert _result(capsys) == {"rule_version": 1, "evaluated": 2, "changed": 2, "skipped_missing_facts": 1}
    with ctx.c.sessionmaker() as db:
        assert {asset_id: _snapshot(db.get(Asset, asset_id)) for asset_id in protected} == before
        assert db.get(Asset, ready.asset_id).quality_status == "WARNING"
        unchanged_facts = db.get(Asset, legacy.asset_id)
        assert (unchanged_facts.quality_status, unchanged_facts.quality_reasons) == (
            "FAILED", ["not_playable", "unknown_unit"],
        )
    assert ctx.audit_actions().count(ACTION) == 1


@pytest.mark.usefixtures("cli_container")
@pytest.mark.parametrize("invalid", ["bad-rule", "multiple-active"])
def test_cli_rejects_invalid_active_rules_without_mutation(
    ctx: Ctx, capsys: pytest.CaptureFixture[str], invalid: str,
) -> None:
    asset = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
    with ctx.c.sessionmaker.begin() as db:
        db.add(asset)
        if invalid == "bad-rule":
            db.get(QualityRuleSet, 1).active = False
            db.add(_rule(2, capture_time_unknown="PASSED"))
        else:
            db.add(_rule(2))
    with ctx.c.sessionmaker() as db:
        before = _snapshot(db.get(Asset, asset.asset_id))

    assert cli.main([COMMAND, ORG_A]) == 1
    captured = capsys.readouterr()
    assert not captured.out and captured.err
    with ctx.c.sessionmaker() as db:
        assert _snapshot(db.get(Asset, asset.asset_id)) == before
    assert ACTION not in ctx.audit_actions()


def test_metadata_only_ingest_uses_active_quality_rules(ctx: Ctx) -> None:
    with ctx.c.sessionmaker.begin() as db:
        db.get(Device, DEV_A_CAMERA).collection_mode = "C0_METADATA_ONLY"
        db.get(QualityRuleSet, 1).active = False
        db.add(_rule(2, capture_time_unknown="FAILED"))

    payload = manifest(mp4_bytes(128), variant="ORIGINAL", timestamp_quality={"source": "UNKNOWN", "verified": False})
    del payload["capture_start_at"]
    del payload["original_timestamps"]
    headers = ctx.as_user(102)
    created = ctx.client.post("/v1/uploads", headers=headers, json=payload)
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "METADATA_ONLY_REGISTERED"
    asset = ctx.client.get(f"/v1/assets/{created.json()['asset_id']}", headers=headers)
    assert asset.status_code == 200, asset.text
    check(asset, "GET", "/v1/assets/{asset_id}")
    assert (asset.json()["quality_status"], asset.json()["quality_reasons"]) == ("FAILED", ["capture_time_unknown"])


def test_received_upload_can_retry_after_invalid_quality_rules(ictx: Ctx) -> None:
    data = mp4_bytes(128)
    asset_id = upload_all(ictx, manifest(data), data, ictx.as_user(102))["asset_id"]
    with ictx.c.sessionmaker.begin() as db:
        received = db.get(Asset, asset_id)
        assert received.ingest_status == "RECEIVED"
        before = _snapshot(received)
        db.get(QualityRuleSet, 1).active = False
        db.add(_rule(2, capture_time_unknown="PASSED"))

    verifier = IntegrityVerifier(ictx.c)
    with pytest.raises(QualityRuleError):
        verifier.verify(asset_id)
    with ictx.c.sessionmaker.begin() as db:
        assert _snapshot(db.get(Asset, asset_id)) == before
        db.get(QualityRuleSet, 2).active = False
        db.add(_rule(3, required_fields={"VIDEO": ["capture_end_at"]}))

    assert verifier.verify(asset_id) == "REGISTERED"
    with ictx.c.sessionmaker() as db:
        registered = db.get(Asset, asset_id)
        assert (registered.quality_status, registered.quality_reasons) == ("FAILED", ["missing:capture_end_at"])
