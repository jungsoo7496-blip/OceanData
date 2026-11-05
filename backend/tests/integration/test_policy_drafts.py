"""09번 온보딩 마법사 — 장비 정책 초안 (요구사항 3.2·3.3·3.4, ADR-0037).

- 카드에서는 구조화된 값(희망 수집 수준·현장 사본 정책·현장 보존 기간)만 미리 채우고 나머지는 '확인 필요'
- 초안은 아무것도 바꾸지 않는다. 운영자(다중 인증)만 적용·반려하고 ORG_ADMIN은 스스로 적용하지 못한다
- 적용은 set-policy와 같은 검사·저장 길로 수집기 정책 새 버전을 쓰고, 초안이 정한 항목을 장비 기록에 맞춘다
- 카드의 '확인 필요' 항목과 연계 난이도 '기준 미정 — 확인 필요'
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import func, select, text

from app.db.models import Device, DevicePolicy, PolicyDraft
from app.seed import DEV_A_CAMERA, DEV_A_COLLECTOR, DEV_A_SENSOR, DEV_B_CAMERA, ORG_A, PRJ_A, fid
from app.services.contracts import contracts_dir
from app.services.device_policy import store_policy
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration
ADMIN_A, MEMBER_A, ADMIN_B, VIEWER, OP, CONSUMER = 101, 102, 103, 105, 106, 109
CONTRACTS = contracts_dir()
EXAMPLE = json.loads((CONTRACTS / "examples/integration-card.vessel-video.json").read_text(encoding="utf-8"))
COLUMNS = ("collection_mode", "local_copy_policy", "local_retention_days", "expected_interval_s", "source_timezone")
DRAFTS, ONE = "/v1/policy-drafts", "/v1/policy-drafts/{policy_draft_id}"
WINDOWS = [{"days": ["MON", "TUE", "WED", "THU", "FRI"], "start_local": "18:00", "end_local": "08:00", "max_kbps": 20000}]


def _state(ctx) -> tuple[dict[str, dict[str, Any]], list[tuple[str, int]]]:  # type: ignore[no-untyped-def]
    """장비 기록(정책 항목)과 저장된 정책 버전 — 초안이 이것을 바꾸지 않는지 본다."""
    with ctx.c.sessionmaker() as db:
        devices = {d.device_id: {k: getattr(d, k) for k in COLUMNS} for d in db.execute(select(Device)).scalars()}
        versions = sorted((r.device_id, r.version) for r in db.execute(select(DevicePolicy)).scalars())
    return devices, versions


def _audit(ctx, action: str) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    with ctx.c.engine.connect() as conn:
        rows = conn.execute(text("SELECT target_id, reason, details FROM audit_log WHERE action = :a ORDER BY seq"),
                            {"a": action}).all()
    return [{"target_id": t, "reason": r, "details": d if isinstance(d, dict) else json.loads(d)} for t, r, d in rows]


def _card(ctx, content: dict[str, Any], device_id: str | None = None, user: int = ADMIN_A) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    body: dict[str, Any] = {"content": content, **({"device_id": device_id} if device_id else {})}
    r = ctx.client.post("/v1/integration-cards", json=body, headers=ctx.as_user(user))
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


def test_card_prefill_review_and_operator_apply(ctx) -> None:
    admin, op = ctx.as_user(ADMIN_A), ctx.as_user(OP, mfa=True)
    # 카드: 희망 수집 수준 C1, 현장 사본 ROTATING·30일, 자유 기록에는 'GPS UTC' 등 (기관 단위 카드 — 장비 기록을 바꾸지 않는다)
    content = {**EXAMPLE, "collection_mode_requested": "C1_SAMPLE_PREVIEW",
               "time_sync": {"note": "GPS UTC, 시간대 Asia/Seoul (가정)"}, "volume": {"note": "1시간마다 (가정)"}}
    card = _card(ctx, content)
    # 요구사항 3.4: 카드의 '확인 필요' 항목, 난이도 등급은 기준 미정
    assert card["open_items"] == [{"section": "equipment", "label": "장비", "state": "NEEDS_CONFIRMATION",
                                   "pointers": ["/equipment/0/manufacturer", "/equipment/0/model"]}]
    assert card["difficulty_grade"] == {"grade": None, "criteria_defined": False, "note": "기준 미정 — 확인 필요"}
    r = ctx.client.get(f"/v1/integration-cards/{card['integration_card_id']}", headers=ctx.as_user(MEMBER_A))
    check(r, "GET", "/v1/integration-cards/{integration_card_id}")
    assert r.json()["open_items"] == card["open_items"]
    before = _state(ctx)
    policy_before = ctx.client.get(f"/v1/devices/{DEV_A_COLLECTOR}/policy", headers=admin).json()

    # 요구사항 3.2: 초안 — 구조화된 카드 값만 미리 채우고, 자유 기록의 시간대·주기는 쓰지 않는다
    r = ctx.client.post(DRAFTS, headers=admin, json={"device_id": DEV_A_CAMERA,
                                                     "integration_card_id": card["integration_card_id"],
                                                     "values": {"allowed_data_roles": ["CORE"]}})
    check(r, "POST", DRAFTS)
    assert r.status_code == 201, r.text
    d = r.json()
    f = d["fields"]
    assert f["collection_mode"] == {"status": "FROM_CARD", "value": "C1_SAMPLE_PREVIEW",
                                    "card_pointer": "/collection_mode_requested"}
    assert f["local_copy_policy"]["status"] == "FROM_CARD" and f["local_copy_policy"]["value"] == "ROTATING"
    assert f["local_retention_days"] == {"status": "FROM_CARD", "value": 30,
                                         "card_pointer": "/current_storage/local_retention_days"}
    assert f["allowed_data_roles"] == {"status": "ENTERED", "value": ["CORE"]}
    for name in ("expected_interval_s", "transfer_windows", "source_timezone"):
        assert f[name] == {"status": "NEEDS_CONFIRMATION"}
    assert d["needs_confirmation"] == ["expected_interval_s", "transfer_windows", "source_timezone"]
    assert (d["status"], d["revision"], d["collector_device_id"], d["integration_card_version"]) == \
        ("DRAFT", 1, DEV_A_COLLECTOR, 1)
    pv = d["preview"]
    assert pv["current_policy_version"] is None and pv["source_in_current_policy"] is True and pv["blockers"] == []
    assert pv["current"]["collection_mode"] == "C2_EVENT_CLIPS"  # 시드 장비 값 (저장본이 없어 기본 정책)
    assert [(c["field"], c["before"], c["after"]) for c in pv["changes"]] == [
        ("collection_mode", "C2_EVENT_CLIPS", "C1_SAMPLE_PREVIEW"), ("local_retention_days", None, 30)]
    draft_path = f"{DRAFTS}/{d['policy_draft_id']}"

    # 장비마다 검토 중인 초안은 하나
    r = ctx.client.post(DRAFTS, headers=admin, json={"device_id": DEV_A_CAMERA})
    check(r, "POST", DRAFTS)
    assert r.status_code == 409 and d["policy_draft_id"] in r.json()["detail"]

    # 고치기: 넣은 값은 ENTERED, '확인 필요'로 되돌리기, revision으로 동시 수정 막기
    r = ctx.client.patch(draft_path, headers=admin, json={
        "revision": 1, "values": {"source_timezone": "Asia/Seoul", "transfer_windows": WINDOWS},
        "needs_confirmation": ["local_retention_days"], "note": "현장 담당자 확인 (합성 메모)"})
    check(r, "PATCH", ONE)
    d = r.json()
    assert r.status_code == 200 and d["revision"] == 2 and d["note"] == "현장 담당자 확인 (합성 메모)"
    assert d["needs_confirmation"] == ["local_retention_days", "expected_interval_s"]
    assert [c["field"] for c in d["preview"]["changes"]] == ["collection_mode", "transfer_windows", "source_timezone"]
    assert next(c for c in d["preview"]["changes"] if c["field"] == "transfer_windows")["scope"] == "COLLECTOR"
    r = ctx.client.patch(draft_path, headers=admin, json={"revision": 1, "values": {"expected_interval_s": 60}})
    check(r, "PATCH", ONE)
    assert r.status_code == 409

    # 요구사항 3.3: 운영자 확인 전에는 아무것도 바뀌지 않고, ORG_ADMIN은 스스로 적용·반려하지 못한다
    assert _state(ctx) == before
    assert ctx.client.get(f"/v1/devices/{DEV_A_COLLECTOR}/policy", headers=admin).json() == policy_before
    for user, code in ((ctx.as_user(ADMIN_A, mfa=True), "FORBIDDEN"), (ctx.as_user(OP), "MFA_REQUIRED"),
                       (ctx.as_user(MEMBER_A, mfa=True), "FORBIDDEN")):
        r = ctx.client.post(f"{draft_path}/apply", headers=user, json={"revision": 2, "expected_policy_version": None})
        check(r, "POST", f"{ONE}/apply")
        assert r.status_code == 403 and r.json()["code"] == code
        assert ctx.client.post(f"{draft_path}/reject", headers=user, json={"reason": "스스로 반려"}).status_code == 403
    r = ctx.client.patch(draft_path, headers=ctx.as_user(MEMBER_A), json={"revision": 2, "values": {}})
    assert r.status_code == 403  # 구성원은 읽기만
    r = ctx.client.get(draft_path, headers=ctx.as_user(MEMBER_A))
    check(r, "GET", ONE)
    assert r.status_code == 200 and r.json()["preview"]["changes"]
    assert _state(ctx) == before

    # 운영자: 검토한 revision이 아니면 409, 맞으면 수집기 정책 새 버전 + 장비 기록 반영
    r = ctx.client.post(f"{draft_path}/apply", headers=op, json={"revision": 1, "expected_policy_version": None})
    check(r, "POST", f"{ONE}/apply")
    assert r.status_code == 409
    r = ctx.client.post(f"{draft_path}/apply", headers=op, json={"revision": 2})  # 본 정책 버전 필수
    check(r, "POST", f"{ONE}/apply")
    assert r.status_code == 400
    r = ctx.client.post(f"{draft_path}/apply", headers=op, json={"revision": 2, "expected_policy_version": None,
                                                                  "reason": "현장 확인 결과 반영"})
    check(r, "POST", f"{ONE}/apply")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "APPLIED" and d["decided_by"] == fid("usr", OP) and "preview" not in d
    assert d["decision_reason"] == "현장 확인 결과 반영"
    applied = d["applied"]
    assert (applied["collector_device_id"], applied["device_policy_version"], applied["policy_version"]) == \
        (DEV_A_COLLECTOR, 1, "pv-1")
    assert {c["field"]: (c["before"], c["after"]) for c in applied["device_changes"]} == {
        "collection_mode": ("C2_EVENT_CLIPS", "C1_SAMPLE_PREVIEW"), "source_timezone": (None, "Asia/Seoul")}

    devices, versions = _state(ctx)
    assert devices[DEV_A_CAMERA] == {**before[0][DEV_A_CAMERA], "collection_mode": "C1_SAMPLE_PREVIEW",
                                     "source_timezone": "Asia/Seoul"}  # 확인 필요(보존 기간·주기)는 그대로
    assert {k: v for k, v in devices.items() if k != DEV_A_CAMERA} == \
        {k: v for k, v in before[0].items() if k != DEV_A_CAMERA}
    assert versions == [(DEV_A_COLLECTOR, 1)]
    pol = ctx.client.get(f"/v1/devices/{DEV_A_COLLECTOR}/policy", headers=admin).json()
    assert pol["policy_version"] == "pv-1" and pol["transfer_windows"] == WINDOWS
    sources = {s["device_id"]: s for s in pol["sources"]}
    assert sources[DEV_A_CAMERA]["collection_mode"] == "C1_SAMPLE_PREVIEW"
    assert sources[DEV_A_CAMERA]["source_timezone"] == "Asia/Seoul"
    assert sources[DEV_A_CAMERA]["allowed_data_roles"] == ["CORE"]
    assert sources[DEV_A_CAMERA]["local_retention_days"] is None
    assert sources[DEV_A_SENSOR] == {**{s["device_id"]: s for s in policy_before["sources"]}[DEV_A_SENSOR]}
    with ctx.c.sessionmaker() as db:
        assert db.get(DevicePolicy, (DEV_A_COLLECTOR, 1)).integration_card_id == card["integration_card_id"]
    linked = ctx.client.get(f"/v1/integration-cards/{card['integration_card_id']}", headers=admin).json()
    assert linked["linked_policies"] == [{"device_id": DEV_A_COLLECTOR, "version": 1}]

    # 끝난 초안은 다시 바뀌지 않는다
    for path, body in ((f"{draft_path}/apply", {"revision": 2, "expected_policy_version": 1}),
                       (f"{draft_path}/reject", {"reason": "이미 적용됨"})):
        r = ctx.client.post(path, headers=op, json=body)
        assert r.status_code == 409 and r.json()["code"] == "CONFLICT"
    r = ctx.client.patch(draft_path, headers=admin, json={"revision": 2, "values": {"expected_interval_s": 60}})
    assert r.status_code == 409
    r = ctx.client.get(draft_path, headers=admin)
    check(r, "GET", ONE)
    assert r.json()["status"] == "APPLIED" and "preview" not in r.json()

    # 감사: 만들기·고치기(전후)·정책 변경(전후 값)·적용
    created = _audit(ctx, "POLICY_DRAFT_CREATED")
    assert created[0]["details"]["fields"]["collection_mode"] == "FROM_CARD"
    updated = _audit(ctx, "POLICY_DRAFT_UPDATED")[0]["details"]
    assert updated["revision"] == {"before": 1, "after": 2}
    note_change = next(x for x in updated["changes"] if x["field"] == "note")
    assert note_change["after"] == {"length": len("현장 담당자 확인 (합성 메모)")}  # 메모 본문은 남기지 않는다
    changed = [a for a in _audit(ctx, "POLICY_CHANGED") if a["details"].get("policy_draft_id")]
    assert len(changed) == 1 and changed[0]["target_id"] == DEV_A_COLLECTOR
    assert changed[0]["reason"] == "현장 확인 결과 반영"
    assert changed[0]["details"]["before"]["collection_mode"] == "C2_EVENT_CLIPS"
    assert changed[0]["details"]["after"]["collection_mode"] == "C1_SAMPLE_PREVIEW"
    assert changed[0]["details"]["after"]["local_retention_days"] is None
    applied_audit = _audit(ctx, "POLICY_DRAFT_APPLIED")
    assert applied_audit[0]["details"]["status"] == {"before": "DRAFT", "after": "APPLIED"}
    assert "DEVICE_LOCAL_COPY_POLICY_CHANGED" not in ctx.audit_actions()  # 현장 사본 정책은 그대로였다


def test_drafts_scope_validation_reject_and_rules(ctx) -> None:
    admin, op = ctx.as_user(ADMIN_A), ctx.as_user(OP, mfa=True)
    # 카드 없이: 모든 항목이 '확인 필요'
    r = ctx.client.post(DRAFTS, headers=admin, json={"device_id": DEV_A_SENSOR})
    check(r, "POST", DRAFTS)
    d = r.json()
    assert r.status_code == 201 and d["needs_confirmation"] == list(d["fields"]) and d["integration_card_id"] is None
    draft_path = f"{DRAFTS}/{d['policy_draft_id']}"

    # 입력 검사: 값의 형식은 장비 정책 계약과 같다
    for body, pointer in (({"values": {"collection_mode": "C9_EVERYTHING"}}, "/values/collection_mode"),
                          ({"values": {"allowed_data_roles": []}}, "/values/allowed_data_roles"),
                          ({"values": {"source_timezone": "Mars/Olympus_Mons"}}, "/values/source_timezone"),
                          ({"values": {"expected_interval_s": 0}}, "/values/expected_interval_s"),
                          ({"values": {"transfer_windows": [{"days": [], "start_local": "25:00"}]}},
                           "/values/transfer_windows/0"),
                          ({"values": {"heartbeat_interval_s": 60}}, "/values/heartbeat_interval_s"),
                          ({"values": {"source_timezone": "UTC"}, "needs_confirmation": ["source_timezone"]},
                           "/needs_confirmation"),
                          ({"needs_confirmation": ["timezone"]}, "/needs_confirmation/0")):
        r = ctx.client.patch(draft_path, headers=admin, json={"revision": 1, **body})
        check(r, "PATCH", ONE)
        assert r.status_code == 400 and r.json()["code"] == "VALIDATION_ERROR", (body, r.text)
        assert any(e["pointer"].startswith(pointer) for e in r.json()["errors"]), (pointer, r.json()["errors"])
    r = ctx.client.post(DRAFTS, headers=admin, json={"device_id": DEV_A_CAMERA, "integration_card_version": 2})
    check(r, "POST", DRAFTS)
    assert r.status_code == 400 and r.json()["errors"][0]["pointer"] == "/integration_card_version"
    assert ctx.client.patch(draft_path, headers=admin, json={"values": {}}).status_code == 400  # revision 필수

    # 기관 격리: B 기관에는 없는 초안, 목록에도 없다. 운영자는 전체를 본다. 역할 밖은 403
    b = ctx.as_user(ADMIN_B)
    assert ctx.client.get(draft_path, headers=b).status_code == 404
    assert ctx.client.patch(draft_path, headers=b, json={"revision": 1, "values": {}}).status_code == 404
    assert "CROSS_ORG_ACCESS_ATTEMPT" in ctx.audit_actions()
    r = ctx.client.get(DRAFTS, headers=b)
    check(r, "GET", DRAFTS)
    assert r.json() == {"items": [], "next_cursor": None}
    assert ctx.client.post(DRAFTS, headers=b, json={"device_id": DEV_A_CAMERA}).status_code == 404
    r = ctx.client.get(DRAFTS, headers=ctx.as_user(OP))
    assert [x["policy_draft_id"] for x in r.json()["items"]] == [d["policy_draft_id"]]
    assert "preview" not in r.json()["items"][0]
    for user in (VIEWER, CONSUMER):
        r = ctx.client.get(DRAFTS, headers=ctx.as_user(user))
        check(r, "GET", DRAFTS)
        assert r.status_code == 403
    r = ctx.client.get(DRAFTS, params={"status": "PENDING"}, headers=admin)
    check(r, "GET", DRAFTS)
    assert r.status_code == 400 and r.json()["errors"][0]["pointer"] == "/query/status"

    # 규칙 위반 (422): 수집기에 매핑되지 않은 원천 장비, 다른 장비의 카드, 다른 기관의 카드(운영자)
    lone = ctx.client.post("/v1/devices", headers=ctx.as_user(ADMIN_A, mfa=True), json={
        "name": "매핑 없는 카메라 (합성)", "kind": "SOURCE", "device_type": "CAMERA", "project_id": PRJ_A,
        "local_copy_policy": "UNKNOWN"}).json()
    r = ctx.client.post(DRAFTS, headers=admin, json={"device_id": lone["device_id"]})
    check(r, "POST", DRAFTS)
    assert r.status_code == 422 and r.json()["unmet_conditions"][0]["actual"] == "매핑 없음"
    sensor_card = _card(ctx, EXAMPLE, device_id=DEV_A_SENSOR)
    r = ctx.client.post(DRAFTS, headers=admin, json={"device_id": DEV_A_CAMERA,
                                                     "integration_card_id": sensor_card["integration_card_id"]})
    assert r.status_code == 422 and r.json()["unmet_conditions"][0]["actual"] == DEV_A_SENSOR
    b_card = _card(ctx, EXAMPLE, user=ADMIN_B)
    r = ctx.client.post(DRAFTS, headers=admin, json={"device_id": DEV_A_CAMERA,
                                                     "integration_card_id": b_card["integration_card_id"]})
    assert r.status_code == 404  # 제공기관에게 다른 기관 카드는 없는 것
    r = ctx.client.post(DRAFTS, headers=ctx.as_user(OP), json={"device_id": DEV_A_CAMERA,
                                                               "integration_card_id": b_card["integration_card_id"]})
    assert r.status_code == 422 and r.json()["unmet_conditions"][0]["condition"] == "장비와 같은 기관의 연계 카드"
    # 수집기에 연결된 카드는 그 수집기의 원천 장비 초안에 쓸 수 있다 (운영자도 초안을 만든다)
    collector_card = _card(ctx, {"basic": {"project_name": "수집기 카드 (합성)"}}, device_id=DEV_A_COLLECTOR)
    r = ctx.client.post(DRAFTS, headers=ctx.as_user(OP), json={
        "device_id": DEV_A_CAMERA, "integration_card_id": collector_card["integration_card_id"]})
    assert r.status_code == 201 and r.json()["organization_id"] == ORG_A
    assert r.json()["needs_confirmation"] == list(r.json()["fields"])  # 카드에 구조화된 값이 없으면 모두 확인 필요
    assert len(collector_card["open_items"]) == 14  # 기본 말고 모두 비어 있음

    # 반려: 운영자·다중 인증·사유 필수. 반려된 초안은 고칠 수 없고, 그 장비에 새 초안을 만들 수 있다
    before = _state(ctx)
    r = ctx.client.post(f"{draft_path}/reject", headers=op, json={"reason": "x"})
    check(r, "POST", f"{ONE}/reject")
    assert r.status_code == 400
    r = ctx.client.post(f"{draft_path}/reject", headers=op, json={"reason": "카드 v2를 받은 뒤 다시 작성"})
    check(r, "POST", f"{ONE}/reject")
    assert r.status_code == 200 and r.json()["status"] == "REJECTED"
    assert r.json()["decision_reason"] == "카드 v2를 받은 뒤 다시 작성" and r.json()["applied"] is None
    assert _state(ctx) == before
    assert ctx.client.patch(draft_path, headers=admin, json={"revision": 1, "values": {}}).status_code == 409
    r = ctx.client.post(f"{draft_path}/apply", headers=op, json={"revision": 1, "expected_policy_version": None})
    assert r.status_code == 409
    rejected = _audit(ctx, "POLICY_DRAFT_REJECTED")
    assert rejected[0]["reason"] == "카드 v2를 받은 뒤 다시 작성"
    assert rejected[0]["details"]["status"] == {"before": "DRAFT", "after": "REJECTED"}
    assert ctx.client.post(DRAFTS, headers=admin, json={"device_id": DEV_A_SENSOR}).status_code == 201
    r = ctx.client.get(DRAFTS, params={"status": "REJECTED", "device_id": DEV_A_SENSOR}, headers=admin)
    check(r, "GET", DRAFTS)
    assert [x["policy_draft_id"] for x in r.json()["items"]] == [d["policy_draft_id"]]
    r = ctx.client.get(DRAFTS, params={"status": "DRAFT", "limit": 1}, headers=admin)
    assert len(r.json()["items"]) == 1 and r.json()["next_cursor"]


def test_apply_keeps_stored_policy_and_refuses_invalid_result(ctx) -> None:
    admin, op = ctx.as_user(ADMIN_A), ctx.as_user(OP, mfa=True)
    # 저장된 정책(감시 규칙·전송 시간대 포함)이 기준이다 — set-policy와 같은 저장 길로 v1을 둔다
    policy = json.loads((CONTRACTS / "examples/device-policy.vessel-camera.json").read_text(encoding="utf-8"))
    doc = json.dumps(policy, ensure_ascii=False)
    for old, new in ((policy["collector_device_id"], DEV_A_COLLECTOR), (policy["sources"][0]["device_id"], DEV_A_CAMERA),
                     (policy["sources"][1]["device_id"], DEV_A_SENSOR), (policy["sources"][0]["project_id"], PRJ_A)):
        doc = doc.replace(old, new)
    stored = json.loads(doc)
    with ctx.c.sessionmaker.begin() as db:
        store_policy(db, DEV_A_COLLECTOR, stored, integration_card_id=None, by="test", now=ctx.c.clock.now())
    d = ctx.client.post(DRAFTS, headers=admin, json={"device_id": DEV_A_SENSOR,
                                                     "values": {"expected_interval_s": 600}}).json()
    assert d["preview"]["current_policy_version"] == 1
    assert d["preview"]["current"]["allowed_data_roles"] == ["CORE", "BYPRODUCT"]  # 저장본 값
    r = ctx.client.post(f"{DRAFTS}/{d['policy_draft_id']}/apply", headers=op,
                        json={"revision": 1, "expected_policy_version": 1})
    assert r.status_code == 200, r.text
    pol = ctx.client.get(f"/v1/devices/{DEV_A_COLLECTOR}/policy", headers=admin).json()
    assert pol["policy_version"] == "pv-2" and pol["watch_rules"] == stored["watch_rules"]
    assert pol["transfer_windows"] == stored["transfer_windows"] and pol["timezone"] == stored["timezone"]
    by_id = {s["device_id"]: s for s in pol["sources"]}
    assert by_id[DEV_A_SENSOR]["expected_interval_s"] == 600
    assert by_id[DEV_A_CAMERA] == {**stored["sources"][0], "observation_mapping_version": None}
    with ctx.c.sessionmaker() as db:
        assert db.get(Device, DEV_A_SENSOR).expected_interval_s == 600

    # 저장본에 지금 규칙으로는 안 되는 항목(다른 기관 장비)이 있으면 적용은 422이고 아무것도 쓰지 않는다
    broken = json.loads(json.dumps(pol))
    broken["sources"].append({**by_id[DEV_A_CAMERA], "device_id": DEV_B_CAMERA})
    with ctx.c.sessionmaker.begin() as db:  # 검사를 거치지 않은 옛 저장본을 흉내 낸다
        db.add(DevicePolicy(device_id=DEV_A_COLLECTOR, version=3, organization_id=ORG_A, policy=broken,
                            integration_card_id=None, created_by="legacy", created_at=ctx.c.clock.now()))
    d = ctx.client.post(DRAFTS, headers=admin, json={"device_id": DEV_A_CAMERA,
                                                     "values": {"collection_mode": "C0_METADATA_ONLY"}}).json()
    assert d["preview"]["blockers"] == [{"condition": "원천 장비는 이 수집기에 매핑된 같은 기관 장비",
                                         "actual": DEV_B_CAMERA}]
    before = _state(ctx)
    r = ctx.client.post(f"{DRAFTS}/{d['policy_draft_id']}/apply", headers=op,
                        json={"revision": 1, "expected_policy_version": d["preview"]["current_policy_version"]})
    check(r, "POST", f"{ONE}/apply")
    assert r.status_code == 422 and r.json()["unmet_conditions"] == d["preview"]["blockers"]
    assert _state(ctx) == before
    with ctx.c.sessionmaker() as db:
        assert db.get(PolicyDraft, d["policy_draft_id"]).status == "DRAFT"
        assert db.execute(select(func.count()).select_from(DevicePolicy)).scalar() == 3


def test_apply_refuses_when_collector_policy_changed_after_preview(ctx) -> None:
    """보안 검토 발견 7: 같은 수집기의 두 초안 — 먼저 적용된 초안의 transfer_windows(수집기 전체)를 다음 적용이 보지 못한 채
    덮어쓰지 않는다. 미리보기에서 본 정책 버전(expected_policy_version)이 다르면 409, 다시 보고 적용하면 된다."""
    admin, op = ctx.as_user(ADMIN_A), ctx.as_user(OP, mfa=True)
    weekend = [{"days": ["SAT", "SUN"], "start_local": "00:00", "end_local": "23:59", "max_kbps": 5000}]
    first = ctx.client.post(DRAFTS, headers=admin, json={"device_id": DEV_A_CAMERA,
                                                         "values": {"transfer_windows": WINDOWS}}).json()
    second = ctx.client.post(DRAFTS, headers=admin, json={"device_id": DEV_A_SENSOR,
                                                          "values": {"transfer_windows": weekend}}).json()
    assert first["preview"]["current_policy_version"] is None and second["preview"]["current_policy_version"] is None
    r = ctx.client.post(f"{DRAFTS}/{first['policy_draft_id']}/apply", headers=op,
                        json={"revision": 1, "expected_policy_version": None})
    assert r.status_code == 200, r.text

    # 두 번째 초안을 그 전에 본 미리보기 기준으로 적용하면 거부, 아무것도 바뀌지 않는다
    before = _state(ctx)
    r = ctx.client.post(f"{DRAFTS}/{second['policy_draft_id']}/apply", headers=op,
                        json={"revision": 1, "expected_policy_version": None})
    check(r, "POST", f"{ONE}/apply")
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT" and "v1" in r.json()["detail"]
    assert _state(ctx) == before
    pol = ctx.client.get(f"/v1/devices/{DEV_A_COLLECTOR}/policy", headers=admin).json()
    assert pol["transfer_windows"] == WINDOWS

    # 다시 보면 지금 정책(v1, 첫 초안의 시간대)과 비교되고, 그 버전으로 적용하면 된다
    again = ctx.client.get(f"{DRAFTS}/{second['policy_draft_id']}", headers=op).json()
    assert again["preview"]["current_policy_version"] == 1
    assert again["preview"]["current"]["transfer_windows"] == WINDOWS
    r = ctx.client.post(f"{DRAFTS}/{second['policy_draft_id']}/apply", headers=op,
                        json={"revision": 1, "expected_policy_version": 1})
    assert r.status_code == 200, r.text
    pol = ctx.client.get(f"/v1/devices/{DEV_A_COLLECTOR}/policy", headers=admin).json()
    assert pol["policy_version"] == "pv-2" and pol["transfer_windows"] == weekend
