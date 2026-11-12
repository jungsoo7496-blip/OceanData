"""09번 온보딩 규칙 (요구사항 3.2·3.4, ADR-0037): 확인 필요 항목, 난이도 기준 미정, 카드 미리 채우기, 초안 → 정책.

미리 채우기는 카드의 구조화된 값 세 개만 쓴다 — 자유 기록에 무엇이 적혀 있어도 결과가 같다(속성 기반).
'확인 필요' 항목은 정책을 바꾸지 않는다(속성 기반).
"""

from __future__ import annotations

import copy
import json

from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain import onboarding as rules
from app.services.contracts import contracts_dir

EXAMPLE = json.loads((contracts_dir() / "examples/integration-card.vessel-video.json").read_text(encoding="utf-8"))
POLICY = json.loads((contracts_dir() / "examples/device-policy.vessel-camera.json").read_text(encoding="utf-8"))
CAM = POLICY["sources"][0]["device_id"]
UNSET_ALL = {f: dict(rules.UNSET) for f in rules.DRAFT_FIELDS}


def test_open_items_empty_sections_and_marked_values() -> None:
    items = {i["section"]: i for i in rules.open_items(EXAMPLE)}
    # 예시 카드: 장비의 제조사·모델이 '확인 필요'로 적혀 있다. 다른 분야는 채워져 있다
    assert items == {"equipment": {"section": "equipment", "label": "장비", "state": "NEEDS_CONFIRMATION",
                                   "pointers": ["/equipment/0/manufacturer", "/equipment/0/model"]}}
    minimal = {"basic": {"project_name": "합성 실증"}}
    sections = [i["section"] for i in rules.open_items(minimal)]
    assert sections == [k for k, _ in rules.CARD_SECTIONS if k != "basic"]  # 부록 A 순서, 모두 EMPTY
    assert {i["state"] for i in rules.open_items(minimal)} == {"EMPTY"}
    # 빈 문자열·빈 목록·빈 값만 있는 객체는 비어 있음. False·0은 답이 있음
    content = {**minimal, "time_sync": {"note": "  "}, "integration_methods": [], "report_forms": {"available": False},
               "volume": {"days": 0}, "network": {"note": "망분리 여부 확인 필요"},
               "current_storage": {"local_copy_policy": "UNKNOWN"}}
    found = {i["section"]: i for i in rules.open_items(content)}
    assert found["time_sync"]["state"] == "EMPTY" and found["integration_methods"]["state"] == "EMPTY"
    assert "report_forms" not in found and "volume" not in found
    assert found["network"] == {"section": "network", "label": "통신", "state": "NEEDS_CONFIRMATION",
                                "pointers": ["/network/note"]}
    # 현장 사본 정책 UNKNOWN(모름)도 확인할 항목이다 (domain.md 9장)
    assert found["current_storage"]["pointers"] == ["/current_storage/local_copy_policy"]


def test_difficulty_grade_is_not_invented() -> None:
    assert rules.DIFFICULTY_GRADE == {"grade": None, "criteria_defined": False, "note": "기준 미정 — 확인 필요"}


def test_prefill_uses_only_structured_card_fields() -> None:
    pre = rules.prefill(EXAMPLE)
    assert pre == {
        "collection_mode": {"status": "FROM_CARD", "value": "C2_EVENT_CLIPS", "card_pointer": "/collection_mode_requested"},
        "local_copy_policy": {"status": "FROM_CARD", "value": "ROTATING",
                              "card_pointer": "/current_storage/local_copy_policy"},
        "local_retention_days": {"status": "FROM_CARD", "value": 30,
                                 "card_pointer": "/current_storage/local_retention_days"},
    }
    assert rules.prefill({"basic": {"project_name": "x"}}) == {}
    assert rules.prefill(None) == {}


# 정책 값처럼 보이는 낱말을 섞은 자유 기록
_POLICY_LIKE = ["C3_APPROVED_FULL", "C0_METADATA_ONLY", "BYPRODUCT", "Asia/Seoul", "3600", "18:00-08:00", "ROTATING",
                "확인 필요", "local_copy_policy: NONE"]
free_text = st.lists(st.sampled_from(_POLICY_LIKE) | st.text(alphabet="C0123_AFULSIaeiou/: 확인필요시각GPS", max_size=8),
                     max_size=5).map(" ".join)


@settings(max_examples=150, deadline=None)
@given(notes=st.dictionaries(st.sampled_from(["data", "time_sync", "network", "core_byproduct", "rights_privacy",
                                              "samples", "termination"]), free_text),
       storage_note=free_text, methods=st.lists(free_text, max_size=3))
def test_prefill_ignores_free_text(notes: dict[str, str], storage_note: str, methods: list[str]) -> None:
    """자유 기록에 정책 값처럼 보이는 글이 있어도 미리 채우기는 구조화된 값만 본다."""
    content = copy.deepcopy(EXAMPLE)
    for key, text in notes.items():
        content[key] = {"note": text}
    content["current_storage"]["note"] = storage_note
    content["integration_methods"] = methods
    assert rules.prefill(content) == rules.prefill(EXAMPLE)
    bare = {"basic": {"project_name": "x"}, **{k: {"note": t} for k, t in notes.items()},
            "integration_methods": methods}
    assert rules.prefill(bare) == {}


def test_initial_and_edited_fields() -> None:
    fields = rules.initial_fields(EXAMPLE, {"expected_interval_s": 3600}, {"local_copy_policy"})
    assert fields["collection_mode"]["status"] == "FROM_CARD"
    assert fields["expected_interval_s"] == {"status": "ENTERED", "value": 3600}
    assert fields["local_copy_policy"] == {"status": "NEEDS_CONFIRMATION"}  # 카드 값이 있어도 사람이 '확인 필요'로 둠
    assert rules.needs_confirmation(fields) == ["allowed_data_roles", "local_copy_policy", "transfer_windows",
                                                "source_timezone"]
    edited, changes = rules.edit_fields(fields, {"source_timezone": None, "expected_interval_s": 3600},
                                        {"collection_mode"})
    assert [c["field"] for c in changes] == ["collection_mode", "source_timezone"]  # 같은 값은 변경 아님
    assert edited["source_timezone"] == {"status": "ENTERED", "value": None}  # null은 '없음'이고 확인 필요가 아니다
    assert edited["collection_mode"] == {"status": "NEEDS_CONFIRMATION"}
    assert fields["collection_mode"]["status"] == "FROM_CARD"  # 원래 값은 바꾸지 않는다


def test_plan_changes_only_this_source_and_collector_windows() -> None:
    fields = {**UNSET_ALL, "collection_mode": {"status": "ENTERED", "value": "C1_SAMPLE_PREVIEW"},
              "allowed_data_roles": {"status": "ENTERED", "value": ["CORE"]},
              "transfer_windows": {"status": "ENTERED", "value": []}}
    p = rules.plan(POLICY, CAM, {"device_id": CAM}, fields)
    assert p.source_in_current is True
    assert [(c["field"], c["scope"]) for c in p.changes] == [("collection_mode", "SOURCE"),
                                                             ("transfer_windows", "COLLECTOR")]
    assert p.policy["sources"][0]["collection_mode"] == "C1_SAMPLE_PREVIEW"
    assert p.policy["sources"][1] == POLICY["sources"][1]  # 다른 원천 장비 항목은 그대로
    assert p.policy["transfer_windows"] == [] and p.policy["watch_rules"] == POLICY["watch_rules"]
    assert p.current["transfer_windows"] == POLICY["transfer_windows"] and POLICY["sources"][0]["collection_mode"] == \
        "C3_APPROVED_FULL"  # 입력 정책은 바꾸지 않는다
    # 허용 역할은 집합: 순서만 다르면 변경 아님
    other = POLICY["sources"][1]["device_id"]
    same = rules.plan(POLICY, other, {}, {**UNSET_ALL, "allowed_data_roles": {"status": "ENTERED",
                                                                             "value": ["BYPRODUCT", "CORE"]}})
    assert same.changes == []


def test_plan_adds_missing_source_from_device_record() -> None:
    new_dev = "dev_01JD0000000000000000009999"
    base = {"device_id": new_dev, "collection_mode": "C1_SAMPLE_PREVIEW", "local_copy_policy": "UNKNOWN",
            "allowed_data_roles": ["CORE"], "source_timezone": None}
    p = rules.plan(POLICY, new_dev, base, {**UNSET_ALL, "source_timezone": {"status": "ENTERED",
                                                                           "value": "Asia/Seoul"}})
    assert p.source_in_current is False
    assert p.policy["sources"][-1] == {**base, "source_timezone": "Asia/Seoul"}
    assert p.current["local_copy_policy"] == "UNKNOWN" and p.current["source_timezone"] is None
    assert [c["field"] for c in p.changes] == ["source_timezone"]


@settings(max_examples=60, deadline=None)
@given(statuses=st.lists(st.booleans(), min_size=len(rules.DRAFT_FIELDS), max_size=len(rules.DRAFT_FIELDS)))
def test_unconfirmed_fields_keep_current_values(statuses: list[bool]) -> None:
    """'확인 필요' 항목은 적용해도 지금 값 그대로다. 정한 항목만 바뀐다."""
    entered = {"collection_mode": "C0_METADATA_ONLY", "allowed_data_roles": ["BYPRODUCT"],
               "local_copy_policy": "NONE", "local_retention_days": 7, "expected_interval_s": 60,
               "transfer_windows": [{"days": ["SUN"], "start_local": "01:00", "end_local": "02:00"}],
               "source_timezone": "UTC"}
    fields = {f: ({"status": "ENTERED", "value": entered[f]} if on else dict(rules.UNSET))
              for f, on in zip(rules.DRAFT_FIELDS, statuses, strict=True)}
    p = rules.plan(POLICY, CAM, {}, fields)
    for f, on in zip(rules.DRAFT_FIELDS, statuses, strict=True):
        assert p.proposed[f] == (entered[f] if on else p.current[f])
    assert {c["field"] for c in p.changes} == {f for f, on in zip(rules.DRAFT_FIELDS, statuses, strict=True) if on}
    if not any(statuses):
        assert p.policy == POLICY
