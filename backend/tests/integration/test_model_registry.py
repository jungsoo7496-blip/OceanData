"""05번 요구사항 8.3·8.4: Model_Registry 등록·조회, 허용 라이선스만, 버전 불변, 등록된 모델만 로드."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.adapters.onnx_inference import ModelRejected, check_model
from app.db.models import ModelRegistryEntry
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration
OP, REVIEWER, ADMIN_A = 106, 108, 101


def test_model_registry_register_list_and_load_check(ctx, tmp_path: Path) -> None:
    weights = tmp_path / "region-detector.onnx"
    weights.write_bytes(b"synthetic-weights-not-a-real-model")
    sha = hashlib.sha256(weights.read_bytes()).hexdigest()
    body = {"name": "region-detector", "version": "0.1.0", "sha256": sha, "license": "Apache-2.0",
            "weights_uri": "region-detector.onnx", "training_data_terms": "합성 시험용 — 실제 모델 아님"}
    op = ctx.as_user(OP, mfa=True)
    assert ctx.client.post("/v1/models", json=body, headers=ctx.as_user(OP)).status_code == 403  # 다중 인증
    assert ctx.client.post("/v1/models", json=body, headers=ctx.as_user(ADMIN_A, mfa=True)).status_code == 403
    r = ctx.client.post("/v1/models", json={**body, "license": "AGPL-3.0-only"}, headers=op)
    assert r.status_code == 422 and r.json()["code"] == "RULE_VIOLATION"  # 허용 목록 밖
    assert ctx.client.post("/v1/models", json={**body, "sha256": "x"}, headers=op).status_code == 400
    assert ctx.client.post("/v1/models", json={**body, "training_data_terms": ""}, headers=op).status_code == 400
    r = ctx.client.post("/v1/models", json=body, headers=op)
    check(r, "POST", "/v1/models")
    assert r.status_code == 201 and r.json()["loadable"] is True and r.json()["approved_by"]
    assert ctx.client.post("/v1/models", json=body, headers=op).status_code == 409  # 버전 불변
    assert "MODEL_REGISTERED" in ctx.audit_actions()

    r = ctx.client.get("/v1/models", headers=ctx.as_user(REVIEWER))
    check(r, "GET", "/v1/models")
    assert [(m["name"], m["version"]) for m in r.json()["items"]] == [("region-detector", "0.1.0")]
    assert "Apache-2.0" in r.json()["allowed_licenses"]
    assert ctx.client.get("/v1/models", headers=ctx.as_user(ADMIN_A)).status_code == 403

    # 로드 판단: 등록·해시가 맞아야 통과, 가중치가 바뀌면 거부, 등록되지 않으면 거부
    with ctx.c.sessionmaker() as db:
        entry = db.get(ModelRegistryEntry, ("region-detector", "0.1.0"))
        check_model(entry, weights)
        weights.write_bytes(b"tampered")
        with pytest.raises(ModelRejected):
            check_model(entry, weights)
        with pytest.raises(ModelRejected):
            check_model(db.get(ModelRegistryEntry, ("unknown", "1")), weights)
