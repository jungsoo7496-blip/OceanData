"""03번 약정·연계 카드 등록 API (요구사항 1.4·1.5) — 계약·권한·기관 격리·공유 규칙 연결·장비 정책 연결."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app import cli
from app.db.models import Device, DevicePolicy
from app.seed import DEV_A_CAMERA, DEV_A_COLLECTOR, DEV_A_SENSOR, DEV_B_CAMERA, ORG_A, ORG_B
from app.services.contracts import contracts_dir
from app.services.sharing import tier2_valid
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_quality_reevaluation import cli_container  # noqa: F401 - 픽스처

pytestmark = pytest.mark.integration
CONTRACTS = contracts_dir()
ADMIN_A, MEMBER_A, ADMIN_B, OP = 101, 102, 103, 106
EXAMPLE = json.loads((CONTRACTS / "examples/integration-card.vessel-video.json").read_text(encoding="utf-8"))
BAD = json.loads((CONTRACTS / "examples/invalid/integration-card.bad-codes.json").read_text(encoding="utf-8"))


def _ts(dt) -> str:  # type: ignore[no-untyped-def]
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_agreements_record_list_end_and_sharing_rule(ctx) -> None:
    now = ctx.c.clock.now()
    op = ctx.as_user(OP, mfa=True)
    body = {"organization_id": ORG_B, "tier": "TIER2", "reference": "합성 2단 약정 제1호",
            "valid_from": _ts(now - timedelta(days=1)), "allowed_purposes": ["AI_TRAINING", "JOINT_RESEARCH"]}
    # 권한: 운영자만, 다중 인증 필요 (공유 가능 여부를 바꾼다)
    assert ctx.client.post("/v1/agreements", json=body, headers=ctx.as_user(ADMIN_B)).status_code == 403
    assert ctx.client.post("/v1/agreements", json=body, headers=ctx.as_user(OP)).status_code == 403
    # 규칙: 1단은 목적 없음, 2단은 목적 하나 이상, 유효기간 순서, 코드표, 시간대, 기관 존재
    r = ctx.client.post("/v1/agreements", json={**body, "tier": "TIER1"}, headers=op)
    assert r.status_code == 422 and r.json()["code"] == "RULE_VIOLATION"
    assert ctx.client.post("/v1/agreements", json={**body, "allowed_purposes": []}, headers=op).status_code == 422
    r = ctx.client.post("/v1/agreements", json={**body, "valid_until": _ts(now - timedelta(days=2))}, headers=op)
    assert r.status_code == 422
    r = ctx.client.post("/v1/agreements", json={**body, "allowed_purposes": ["MIND_READING"]}, headers=op)
    assert r.status_code == 400 and r.json()["code"] == "VALIDATION_ERROR"
    r = ctx.client.post("/v1/agreements", json={**body, "valid_from": "2026-11-01T00:00:00"}, headers=op)
    assert r.status_code == 400
    r = ctx.client.post("/v1/agreements", json={**body, "organization_id": "org_0000000000000000000000ZZZZ"},
                        headers=op)
    assert r.status_code == 404
    with ctx.c.sessionmaker() as db:
        assert tier2_valid(db, ORG_B, now) is None

    r = ctx.client.post("/v1/agreements", json=body, headers=op)
    check(r, "POST", "/v1/agreements")
    a = r.json()
    assert r.status_code == 201 and a["in_effect"] is True and a["allowed_purposes"] == ["AI_TRAINING", "JOINT_RESEARCH"]
    with ctx.c.sessionmaker() as db:  # 13번 공유 규칙이 이 기록을 그대로 쓴다
        assert tier2_valid(db, ORG_B, now).agreement_id == a["agreement_id"]
    r = ctx.client.post("/v1/agreements", headers=op, json={"organization_id": ORG_A, "tier": "TIER1",
                                                            "reference": "합성 1단 약정", "valid_from": body["valid_from"]})
    assert r.status_code == 201

    # 조회 범위: 제공기관은 자기 기관만, 운영자는 전체
    r = ctx.client.get("/v1/agreements", headers=ctx.as_user(ADMIN_B))
    check(r, "GET", "/v1/agreements")
    assert [x["agreement_id"] for x in r.json()["items"]] == [a["agreement_id"]]
    assert {x["organization_id"] for x in ctx.client.get("/v1/agreements", headers=ctx.as_user(MEMBER_A))
            .json()["items"]} == {ORG_A}
    assert {x["organization_id"] for x in ctx.client.get("/v1/agreements", headers=op).json()["items"]} >= \
        {ORG_A, ORG_B}
    assert ctx.client.get("/v1/agreements", headers=ctx.as_user(109)).status_code == 403  # 이용기관

    # 종료: valid_until을 당긴다 → 2단 약정 무효, 다시 종료는 409, 시작 전으로는 못 당긴다
    path = f"/v1/agreements/{a['agreement_id']}/end"
    r = ctx.client.post(path, json={"reason": "x", "effective_at": _ts(now - timedelta(days=3))}, headers=op)
    assert r.status_code == 400  # 사유 2자 미만
    r = ctx.client.post(path, json={"reason": "시작 전", "effective_at": _ts(now - timedelta(days=3))}, headers=op)
    assert r.status_code == 422
    assert ctx.client.post(path, json={"reason": "계약 종료"}, headers=ctx.as_user(OP)).status_code == 403
    r = ctx.client.post(path, json={"reason": "계약 종료"}, headers=op)
    check(r, "POST", "/v1/agreements/{agreement_id}/end")
    assert r.status_code == 200 and r.json()["in_effect"] is False
    with ctx.c.sessionmaker() as db:
        assert tier2_valid(db, ORG_B, now + timedelta(seconds=1)) is None
    assert ctx.client.post(path, json={"reason": "다시"}, headers=op).status_code == 409
    assert ctx.client.post("/v1/agreements/agr_0000000000000000000000ZZZZ/end", json={"reason": "없음"},
                           headers=op).status_code == 404
    actions = ctx.audit_actions()
    assert actions.count("AGREEMENT_RECORDED") == 2 and actions.count("AGREEMENT_ENDED") == 1


def test_integration_cards_versions_device_policy_and_isolation(ctx, cli_container, tmp_path) -> None:  # noqa: F811
    admin = ctx.as_user(ADMIN_A)
    with ctx.c.sessionmaker.begin() as db:  # 시작 상태: 현장 원본을 오래 보관하는 장비
        db.get(Device, DEV_A_CAMERA).local_copy_policy = "RETAINED"
        mode_before = db.get(Device, DEV_A_CAMERA).collection_mode
    wish = {**EXAMPLE, "collection_mode_requested": "C3_APPROVED_FULL"}  # 장비 현재 수준과 다른 희망
    assert mode_before != wish["collection_mode_requested"]
    # 권한: ORG_ADMIN만 기록 (구성원·운영자 불가)
    for user in (MEMBER_A, OP):
        assert ctx.client.post("/v1/integration-cards", json={"content": EXAMPLE},
                               headers=ctx.as_user(user, mfa=True)).status_code == 403
    # 계약 검사: 코드표 밖 값·실명 담당자·TOP 3 초과 → 400, 어느 필드인지 알려 준다
    r = ctx.client.post("/v1/integration-cards", json={"content": BAD}, headers=admin)
    assert r.status_code == 400 and r.json()["code"] == "VALIDATION_ERROR"
    pointers = {e["pointer"] for e in r.json()["errors"]}
    assert {"/content/collection_mode_requested", "/content/current_storage/local_copy_policy"} <= pointers
    # 다른 기관 장비는 없는 것으로 (교차 기관 시도 감사)
    r = ctx.client.post("/v1/integration-cards", json={"device_id": DEV_B_CAMERA, "content": EXAMPLE}, headers=admin)
    assert r.status_code == 404

    r = ctx.client.post("/v1/integration-cards", json={"device_id": DEV_A_CAMERA, "content": wish}, headers=admin)
    check(r, "POST", "/v1/integration-cards")
    card = r.json()
    assert r.status_code == 201 and card["version"] == 1 and card["organization_id"] == ORG_A
    # 카드의 현장 사본 정책이 장비 기록에 반영되고 '센터 원본이 유일본일 수 있음' 안내가 켜진다 (domain.md 9장)
    with ctx.c.sessionmaker() as db:
        cam = db.get(Device, DEV_A_CAMERA)
        assert cam.local_copy_policy == "ROTATING"
        assert cam.collection_mode == mode_before  # 희망 collection_mode는 기록만 한다 (운영기관 결정)
    dev = ctx.client.get(f"/v1/devices/{DEV_A_CAMERA}", headers=admin).json()
    assert dev["local_copy_policy"] == "ROTATING" and dev["center_copy_may_be_only_copy"] is True

    # 새 버전: 이전 버전은 그대로
    v2_content = {**EXAMPLE, "current_storage": {"local_copy_policy": "RETAINED", "note": "NAS 장기 보관으로 바뀜"}}
    cpath = f"/v1/integration-cards/{card['integration_card_id']}"
    r = ctx.client.post(cpath + "/versions", json={"device_id": DEV_A_CAMERA, "content": v2_content}, headers=admin)
    check(r, "POST", "/v1/integration-cards/{integration_card_id}/versions")
    assert r.status_code == 201 and r.json()["version"] == 2
    r = ctx.client.get(cpath, params={"version": 1}, headers=ctx.as_user(MEMBER_A))
    check(r, "GET", "/v1/integration-cards/{integration_card_id}")
    assert r.json()["content"]["current_storage"]["local_copy_policy"] == "ROTATING"
    assert ctx.client.get(cpath, headers=admin).json()["version"] == 2
    with ctx.c.sessionmaker() as db:
        assert db.get(Device, DEV_A_CAMERA).local_copy_policy == "RETAINED"
    assert ctx.client.get(cpath, params={"version": 9}, headers=admin).status_code == 404

    # 기관 격리: B 기관에는 없는 카드, 목록에도 없다. 운영자는 전체 목록을 본다
    assert ctx.client.get(cpath, headers=ctx.as_user(ADMIN_B)).status_code == 404
    assert ctx.client.post(cpath + "/versions", json={"content": EXAMPLE},
                           headers=ctx.as_user(ADMIN_B)).status_code == 404
    assert "CROSS_ORG_ACCESS_ATTEMPT" in ctx.audit_actions()
    r = ctx.client.get("/v1/integration-cards", headers=ctx.as_user(ADMIN_B))
    check(r, "GET", "/v1/integration-cards")
    assert r.json()["items"] == []
    items = ctx.client.get("/v1/integration-cards", headers=ctx.as_user(OP)).json()["items"]
    assert [(x["integration_card_id"], x["version"]) for x in items] == [(card["integration_card_id"], 2)]

    # 카드에서 만든 장비 정책과 연결 (요구사항 1.5): set-policy --integration-card
    policy = json.loads((CONTRACTS / "examples/device-policy.vessel-camera.json").read_text(encoding="utf-8"))
    text = json.dumps(policy, ensure_ascii=False)  # 예시의 장비 ID를 시드 장비로 바꾼다 (규칙의 참조까지 함께)
    for old_id, new_id in ((policy["collector_device_id"], DEV_A_COLLECTOR),
                           (policy["sources"][0]["device_id"], DEV_A_CAMERA),
                           (policy["sources"][1]["device_id"], DEV_A_SENSOR)):
        text = text.replace(old_id, new_id)
    pfile = tmp_path / "policy.json"
    pfile.write_text(text, encoding="utf-8")
    other = ctx.client.post("/v1/integration-cards", json={"content": EXAMPLE},
                            headers=ctx.as_user(ADMIN_B)).json()["integration_card_id"]
    assert cli.main(["set-policy", DEV_A_COLLECTOR, str(pfile), "--integration-card", other]) == 1
    assert cli.main(["set-policy", DEV_A_COLLECTOR, str(pfile), "--integration-card",
                     card["integration_card_id"]]) == 0
    with ctx.c.sessionmaker() as db:
        stored = db.query(DevicePolicy).filter_by(device_id=DEV_A_COLLECTOR).one()
        assert stored.integration_card_id == card["integration_card_id"]
    linked = ctx.client.get(cpath, headers=admin).json()["linked_policies"]
    assert linked == [{"device_id": DEV_A_COLLECTOR, "version": stored.version}]
    actions = ctx.audit_actions()
    assert actions.count("INTEGRATION_CARD_RECORDED") == 3 and actions.count("DEVICE_LOCAL_COPY_POLICY_CHANGED") == 2
