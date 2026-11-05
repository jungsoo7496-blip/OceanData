"""06번·ADR-0040: 장비 정책 숫자 상한을 저장 길(set-policy·초안·장비 등록)이 모두 지키고, 상한 전에 저장된 값도
수집기 상태 조회를 깨지 않는다.

에이전트는 heartbeat_interval_s 등을 time.Duration으로 곱한다. 상한이 없을 때는 아주 큰 값이 넘쳐 하트비트 루프가
쉬지 않고 돌았다(퍼즈로 찾음). 서버는 그런 정책을 처음부터 저장하지 않는다.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

from app import cli
from app.db.models import Device, DevicePolicy
from app.domain.errors import RuleViolation
from app.seed import DEV_A_CAMERA, DEV_A_COLLECTOR, DEV_A_SENSOR, ORG_A, PRJ_A
from app.services.contracts import contracts_dir
from app.services.device_policy import store_policy
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_agents import _device_headers, hb
from tests.integration.test_quality_reevaluation import cli_container  # noqa: F401 - 픽스처

pytestmark = pytest.mark.integration
ADMIN_A = 101
CONTRACTS = contracts_dir()
INT32_MAX = 2**31 - 1
# 상한 위치와 값 (contracts/schemas/device-policy.schema.json maximum)
LIMITS: dict[tuple[str | int, ...], int] = {
    ("heartbeat_interval_s",): INT32_MAX, ("size_stable_s",): INT32_MAX, ("max_parallel_parts",): 10000,
    ("sources", 0, "expected_interval_s"): INT32_MAX, ("sources", 0, "local_retention_days"): 24855,
    ("transfer_windows", 0, "max_kbps"): INT32_MAX, ("watch_rules", 0, "priority"): INT32_MAX,
}


def _example(name: str) -> dict[str, Any]:
    """계약 예시 정책의 장비·사업 ID를 시드 값으로 바꾼다 (감시 규칙의 참조까지 함께)."""
    policy = json.loads((CONTRACTS / name).read_text(encoding="utf-8"))
    doc = json.dumps(policy, ensure_ascii=False)
    for old, new in ((policy["collector_device_id"], DEV_A_COLLECTOR), (policy["sources"][0]["device_id"], DEV_A_CAMERA),
                     (policy["sources"][1]["device_id"], DEV_A_SENSOR), (policy["sources"][0]["project_id"], PRJ_A)):
        doc = doc.replace(old, new)
    return json.loads(doc)  # type: ignore[no-any-return]


def _set(doc: dict[str, Any], path: tuple[str | int, ...], value: int) -> dict[str, Any]:
    out = copy.deepcopy(doc)
    node: Any = out
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return out


def _policy_count(ctx) -> int:  # type: ignore[no-untyped-def]
    with ctx.c.sessionmaker() as db:
        return int(db.execute(select(func.count()).select_from(DevicePolicy)).scalar() or 0)


def test_policy_numbers_above_contract_maximum_are_refused(ctx, cli_container, tmp_path, capsys) -> None:  # noqa: F811
    admin = ctx.as_user(ADMIN_A)
    # 상한 값은 저장되고 정책 응답도 계약에 맞는다
    top = _example("examples/device-policy.vessel-camera.json")
    for path, limit in LIMITS.items():
        top = _set(top, path, limit)
    with ctx.c.sessionmaker.begin() as db:
        assert store_policy(db, DEV_A_COLLECTOR, top, integration_card_id=None, by="test",
                            now=ctx.c.clock.now()).version == 1
    r = ctx.client.get(f"/v1/devices/{DEV_A_COLLECTOR}/policy", headers=admin)
    check(r, "GET", "/v1/devices/{device_id}/policy")
    assert r.json()["heartbeat_interval_s"] == INT32_MAX and r.json()["max_parallel_parts"] == 10000

    # set-policy: 계약의 실패 예시(Go time.Duration을 넘치는 하트비트 주기)는 저장하지 않고 어긴 항목을 알려 준다
    pfile = tmp_path / "policy.json"
    pfile.write_text(json.dumps(_example("examples/invalid/device-policy.heartbeat-interval-overflow.json")),
                     encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["set-policy", DEV_A_COLLECTOR, str(pfile)]) == 1
    assert "장비 정책 계약 /heartbeat_interval_s" in capsys.readouterr().err
    # 저장 길(set-policy·초안 적용이 함께 쓰는 store_policy): 상한보다 1만 커도 거부, 아무것도 쓰지 않는다
    for path, limit in LIMITS.items():
        with pytest.raises(RuleViolation) as exc, ctx.c.sessionmaker.begin() as db:
            store_policy(db, DEV_A_COLLECTOR, _set(top, path, limit + 1), integration_card_id=None, by="test",
                         now=ctx.c.clock.now())
        pointer = "/" + "/".join(map(str, path))
        assert [u.condition for u in exc.value.unmet] == [f"장비 정책 계약 {pointer}"], path
    assert _policy_count(ctx) == 1

    # 정책 초안의 값도 같은 계약 항목으로 검사한다 (400, 초안을 만들지 않음)
    for values, pointer in (({"expected_interval_s": INT32_MAX + 1}, "/values/expected_interval_s"),
                            ({"local_retention_days": 24856}, "/values/local_retention_days"),
                            ({"transfer_windows": [{"days": ["MON"], "start_local": "18:00", "end_local": "08:00",
                                                    "max_kbps": INT32_MAX + 1}]}, "/values/transfer_windows/0/max_kbps")):
        r = ctx.client.post("/v1/policy-drafts", headers=admin, json={"device_id": DEV_A_SENSOR, "values": values})
        check(r, "POST", "/v1/policy-drafts")
        assert r.status_code == 400 and [e["pointer"] for e in r.json()["errors"]] == [pointer], r.text

    # 장비 등록 값은 기본 정책의 sources 항목이 되므로 같은 상한이다 — 넘으면 400이라 정책 응답이 계약을 어기지 않는다
    # (integer 열을 넘는 값은 전에도 DB 범위 오류 400이었지만 24856일 같은 값은 받았다)
    device = {"name": "상한 시험 카메라 (합성)", "kind": "SOURCE", "device_type": "CAMERA", "project_id": PRJ_A,
              "collector_device_id": DEV_A_COLLECTOR, "local_copy_policy": "RETAINED"}
    org_admin = ctx.as_user(ADMIN_A, mfa=True)
    for extra in ({"expected_interval_s": INT32_MAX + 1}, {"local_retention_days": 24856},
                  {"local_retention_days": INT32_MAX + 1}):
        r = ctx.client.post("/v1/devices", headers=org_admin, json={**device, **extra})
        check(r, "POST", "/v1/devices")
        assert r.status_code == 400 and r.json()["code"] == "VALIDATION_ERROR", (extra, r.text)
    r = ctx.client.post("/v1/devices", headers=org_admin,
                        json={**device, "expected_interval_s": INT32_MAX, "local_retention_days": 24855})
    check(r, "POST", "/v1/devices")
    assert r.status_code == 201, r.text


def test_health_survives_heartbeat_interval_stored_before_the_bound(ctx) -> None:
    """상한이 생기기 전에 (검사 없이) 저장된 옛 정책의 아주 큰 주기도 '마지막 하트비트 + 주기' 계산을 넘치게 하지 않는다."""
    legacy = {**_example("examples/device-policy.vessel-camera.json"), "heartbeat_interval_s": 10**15}
    with ctx.c.sessionmaker.begin() as db:  # 검사를 거치지 않은 옛 저장본을 흉내 낸다
        db.add(DevicePolicy(device_id=DEV_A_COLLECTOR, version=1, organization_id=ORG_A, policy=legacy,
                            integration_card_id=None, created_by="legacy", created_at=ctx.c.clock.now()))
    assert ctx.client.post("/v1/agents/heartbeat", json=hb(policy_version="pv-11"),
                           headers=_device_headers(ctx)).status_code == 204
    r = ctx.client.get(f"/v1/agents/{DEV_A_COLLECTOR}/health", headers=ctx.as_user(102))
    check(r, "GET", "/v1/agents/{device_id}/health")
    assert r.status_code == 200 and r.json()["policy_mismatch"] is True, r.text
    health = r.json()
    assert datetime.fromisoformat(health["next_expected_at"]) - datetime.fromisoformat(
        health["last_heartbeat_at"]) == timedelta(seconds=60)
    r = ctx.client.get(f"/v1/devices/{DEV_A_COLLECTOR}/policy", headers=ctx.as_user(102))
    assert r.status_code == 422 and r.json()["code"] == "RULE_VIOLATION"
    assert r.json()["unmet_conditions"][0]["condition"].endswith("/heartbeat_interval_s")
    check(r, "GET", "/v1/devices/{device_id}/policy")


def test_legacy_device_and_card_values_are_visible_but_not_published(ctx) -> None:
    """이전 장비 값은 미리보기에 남고, 상한을 넘는 카드 값은 초안에 자동 반영되지 않는다."""
    with ctx.c.sessionmaker.begin() as db:
        device = db.get(Device, DEV_A_CAMERA)
        assert device is not None
        device.local_retention_days = 24856
    admin = ctx.as_user(ADMIN_A)
    policy_url = f"/v1/devices/{DEV_A_COLLECTOR}/policy"
    r = ctx.client.get(policy_url, headers=admin)
    assert r.status_code == 422
    check(r, "GET", "/v1/devices/{device_id}/policy")

    card = json.loads((CONTRACTS / "examples/integration-card.vessel-video.json").read_text(encoding="utf-8"))
    card["current_storage"]["local_retention_days"] = 24856
    r = ctx.client.post("/v1/integration-cards", headers=admin, json={"content": card})
    assert r.status_code == 201
    r = ctx.client.post("/v1/policy-drafts", headers=admin, json={
        "device_id": DEV_A_CAMERA, "integration_card_id": r.json()["integration_card_id"],
    })
    assert r.status_code == 201
    draft = r.json()
    assert draft["fields"]["local_retention_days"] == {"status": "NEEDS_CONFIRMATION"}
    assert draft["preview"]["current"]["local_retention_days"] == 24856
    assert draft["preview"]["blockers"]
    check(r, "POST", "/v1/policy-drafts")

    path = f"/v1/policy-drafts/{draft['policy_draft_id']}"
    r = ctx.client.patch(path, headers=admin, json={"revision": 1, "values": {"local_retention_days": 30}})
    assert r.status_code == 200
    check(r, "PATCH", "/v1/policy-drafts/{policy_draft_id}")
    r = ctx.client.post(f"{path}/apply", headers=ctx.as_user(106, mfa=True),
                        json={"revision": 2, "expected_policy_version": None, "reason": "합성 정책 정정"})
    assert r.status_code == 200
    r = ctx.client.get(policy_url, headers=admin)
    assert r.status_code == 200
    check(r, "GET", "/v1/devices/{device_id}/policy")
