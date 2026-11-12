"""분류가 바뀐 뒤 둘 수 있는 등급 (계약 점검 S11, ADR-0074): 이번 변경이 새로 깨는 조건만 본다."""

from __future__ import annotations

from dataclasses import replace

from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.sharing_rules import LEVEL, SCOPES, SENSITIVITY_ORDER, Facts, Lineal, evaluate, scope_after_change

ALL = {"ORG_ADMIN", "PLATFORM_OPERATOR"}


def good(**kw) -> Facts:
    base = dict(variant="DEID", data_role="BYPRODUCT", sensitivity_class="INTERNAL", privacy_status="NOT_APPLICABLE",
                review_result="PASSED", share_status="APPROVED", legal_basis_ref="LB-1", tier2_agreement_valid=True,
                in_dataset_version=True, ancestors=(Lineal("ORIGINAL", "BYPRODUCT", "INTERNAL"),))
    base.update(kw)
    return Facts(**base)  # type: ignore[arg-type]


def test_security_drops_shared_levels_to_l1() -> None:
    f = good()
    for current in ("SANDBOX_PARTNERS", "APPROVED_PARTNERS", "PUBLIC"):
        assert scope_after_change(current, f, replace(f, sensitivity_class="SECURITY")) == "PROGRAM_REVIEW"
    # 원본(조상)이 SECURITY가 되면 파생본도 물려받는다
    after = replace(f, ancestors=(Lineal("ORIGINAL", "BYPRODUCT", "SECURITY"),))
    assert scope_after_change("SANDBOX_PARTNERS", f, after) == "PROGRAM_REVIEW"
    assert scope_after_change("PROGRAM_REVIEW", f, after) == "PROGRAM_REVIEW"
    assert scope_after_change("PRIVATE", f, after) == "PRIVATE"


def test_restricted_leaves_l4_for_the_highest_level_whose_conditions_hold() -> None:
    f = good()
    assert scope_after_change("PUBLIC", f, replace(f, sensitivity_class="RESTRICTED")) == "APPROVED_PARTNERS"
    f = good(in_dataset_version=False)  # L3은 데이터셋 버전에 들어 있어야 한다 — 그 조건을 못 맞추면 L2
    assert scope_after_change("PUBLIC", f, replace(f, sensitivity_class="RESTRICTED")) == "SANDBOX_PARTNERS"
    assert scope_after_change("SANDBOX_PARTNERS", f, replace(f, sensitivity_class="RESTRICTED")) == "SANDBOX_PARTNERS"


def test_conditions_unmet_before_the_change_are_not_its_concern() -> None:
    f = good(tier2_agreement_valid=False)  # 약정 만료는 이용 권한을 줄 때 본다
    assert scope_after_change("SANDBOX_PARTNERS", f, replace(f, privacy_status="ANONYMIZED_VERIFIED")) == (
        "SANDBOX_PARTNERS")
    assert scope_after_change("SANDBOX_PARTNERS", f, replace(f, sensitivity_class="SECURITY")) == "PROGRAM_REVIEW"


SENS = st.sampled_from(sorted(SENSITIVITY_ORDER))
PRIV = st.sampled_from(["UNKNOWN", "NOT_APPLICABLE", "REVIEW_REQUIRED", "PSEUDONYMIZED", "ANONYMIZED_VERIFIED"])


@settings(max_examples=300, deadline=None)
@given(current=st.sampled_from(SCOPES), s0=SENS, s1=SENS, p0=PRIV, p1=PRIV, anc0=SENS, anc1=SENS,
       tier2=st.booleans(), in_ds=st.booleans(), review=st.sampled_from([None, "PASSED", "FAILED"]))
def test_result_never_rises_and_adds_no_unmet_condition(current: str, s0: str, s1: str, p0: str, p1: str, anc0: str,
                                                        anc1: str, tier2: bool, in_ds: bool, review: str | None) -> None:
    before = good(sensitivity_class=s0, privacy_status=p0, tier2_agreement_valid=tier2, in_dataset_version=in_ds,
                  review_result=review, ancestors=(Lineal("ORIGINAL", "BYPRODUCT", anc0),))
    after = replace(before, sensitivity_class=s1, privacy_status=p1, ancestors=(Lineal("ORIGINAL", "BYPRODUCT", anc1),))
    got = scope_after_change(current, before, after)
    assert LEVEL[got] <= LEVEL[current]
    known = {u.condition for u in evaluate(current, before, ALL)}
    assert {u.condition for u in evaluate(got, after, ALL)} <= known
    assert scope_after_change(current, before, before) == current  # 바뀐 것이 없으면 그대로
