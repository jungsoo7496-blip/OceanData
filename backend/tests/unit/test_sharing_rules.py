"""T08 공유 조건 (domain.md 4장): 예시 시험 + 무작위 속성·파생 관계 조합 속성 기반 시험 (13번 요구사항 3.4, 3.7)."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.sharing_rules import (
    LEVEL,
    SCOPES,
    Facts,
    Lineal,
    dataset_scope,
    evaluate,
    max_scope_for,
)

ALL = {"ORG_ADMIN", "PLATFORM_OPERATOR"}


def good(**kw) -> Facts:
    base = dict(variant="DEID", data_role="BYPRODUCT", sensitivity_class="INTERNAL", privacy_status="PSEUDONYMIZED",
                review_result="PASSED", share_status="APPROVED", legal_basis_ref="LB-1", tier2_agreement_valid=True,
                in_dataset_version=True,
                ancestors=(Lineal("ORIGINAL", "BYPRODUCT", "INTERNAL"),))
    base.update(kw)
    return Facts(**base)  # type: ignore[arg-type]


def conds(target: str, f: Facts, approvals=ALL) -> set[str]:
    return {u.condition for u in evaluate(target, f, approvals)}


def test_happy_paths() -> None:
    f = good()
    assert evaluate("SANDBOX_PARTNERS", f, {"ORG_ADMIN"}) == []
    assert evaluate("APPROVED_PARTNERS", f, ALL) == []
    assert conds("PUBLIC", f) == {"privacy_status ∈ {NOT_APPLICABLE, ANONYMIZED_VERIFIED}"}  # 가명은 L4 불가
    assert evaluate("PUBLIC", good(privacy_status="ANONYMIZED_VERIFIED"), ALL) == []


def test_t08_a_core_cannot_reach_l2() -> None:
    assert "data_role=BYPRODUCT (원 자산 포함)" in conds("SANDBOX_PARTNERS", good(data_role="CORE"))


def test_t08_b_unreviewed_deid_cannot_reach_l2() -> None:
    assert "review_result=PASSED" in conds("SANDBOX_PARTNERS", good(review_result=None))


def test_t08_d_agreement_required() -> None:
    assert "제공기관 2단 약정 유효" in conds("SANDBOX_PARTNERS", good(tier2_agreement_valid=False))


def test_t08_h_original_and_core_original_derivatives() -> None:
    assert "variant ≠ ORIGINAL" in conds("SANDBOX_PARTNERS", good(variant="ORIGINAL", ancestors=()))
    core = good(ancestors=(Lineal("ORIGINAL", "CORE", "INTERNAL"),))
    assert {"CORE 원본의 파생본이 아님", "data_role=BYPRODUCT (원 자산 포함)"} <= conds("SANDBOX_PARTNERS", core)


def test_t08_i_operator_final_approval_required() -> None:
    assert "share_status=APPROVED (운영자 최종 승인)" in conds("SANDBOX_PARTNERS", good(share_status="QUALITY_REVIEW"))


def test_t08_j_l1_needs_org_admin() -> None:
    assert conds("PROGRAM_REVIEW", good(), approvals={"PLATFORM_OPERATOR"}) == {"ORG_ADMIN 승인"}
    assert evaluate("PROGRAM_REVIEW", good(variant="ORIGINAL", data_role="CORE"), {"ORG_ADMIN"}) == []


def test_l3_needs_dataset_and_operator() -> None:
    assert conds("APPROVED_PARTNERS", good(in_dataset_version=False), {"ORG_ADMIN"}) == \
        {"데이터셋 버전에 포함", "PLATFORM_OPERATOR 승인"}


def test_sensitivity_inherited_strictest() -> None:
    f = good(ancestors=(Lineal("ORIGINAL", "BYPRODUCT", "SECURITY"),))
    assert f.effective_sensitivity == "SECURITY" and "sensitivity_class ≠ SECURITY" in conds("SANDBOX_PARTNERS", f)


def test_dataset_scope_is_lowest() -> None:
    assert dataset_scope(["PUBLIC", "APPROVED_PARTNERS", "PUBLIC"]) == "APPROVED_PARTNERS"
    assert dataset_scope(["PUBLIC", "PUBLIC"]) == "PUBLIC"


# ------------------------------------------------------------------ 속성 기반 (T08-c, T08-g)

VARIANTS = ["ORIGINAL", "PROXY", "THUMBNAIL", "SAMPLE_FRAME", "CLIP", "DEID", "LABEL", "SENSOR_EXPORT"]
lineal = st.builds(Lineal, st.sampled_from(VARIANTS), st.sampled_from(["CORE", "BYPRODUCT"]),
                   st.sampled_from(["INTERNAL", "RESTRICTED", "SECURITY"]))
facts = st.builds(
    Facts,
    variant=st.sampled_from(VARIANTS),
    data_role=st.sampled_from(["CORE", "BYPRODUCT"]),
    sensitivity_class=st.sampled_from(["INTERNAL", "RESTRICTED", "SECURITY"]),
    privacy_status=st.sampled_from(["UNKNOWN", "NOT_APPLICABLE", "REVIEW_REQUIRED", "PSEUDONYMIZED",
                                    "ANONYMIZED_VERIFIED"]),
    review_result=st.sampled_from([None, "PASSED", "FAILED"]),
    share_status=st.sampled_from(["NONE", "CANDIDATE", "LEGAL_REVIEW", "PROCESSING", "QUALITY_REVIEW", "APPROVED",
                                  "REJECTED", "REPROCESS"]),
    legal_basis_ref=st.sampled_from([None, "", "LB-1"]),
    tier2_agreement_valid=st.booleans(),
    in_dataset_version=st.booleans(),
    ancestors=st.lists(lineal, max_size=4).map(tuple),  # 원본 → 비식별본 → 데이터셋 사본 같은 사슬
)
approvals = st.sets(st.sampled_from(["ORG_ADMIN", "PLATFORM_OPERATOR", "REVIEWER", "ORG_MEMBER"]))


@settings(max_examples=3000, deadline=None)
@given(f=facts, target=st.sampled_from(SCOPES), ok=approvals)
def test_t08_c_no_path_breaks_domain_rules(f: Facts, target: str, ok: set[str]) -> None:
    """조건을 모두 충족했다고 판정되면, domain.md 4장 불변식이 반드시 성립한다."""
    if evaluate(target, f, ok):
        return
    level = LEVEL[target]
    if level >= 1:
        assert "ORG_ADMIN" in ok
    if level >= 2:
        roles = [f.data_role, *(a.data_role for a in f.ancestors)]
        assert "CORE" not in roles  # CORE·CORE 원본의 파생본은 L1을 넘지 않는다
        assert f.variant != "ORIGINAL"  # 원본은 L1을 넘지 않는다
        assert f.review_result == "PASSED" and f.share_status == "APPROVED"
        assert f.privacy_status in {"NOT_APPLICABLE", "PSEUDONYMIZED", "ANONYMIZED_VERIFIED"}
        assert all(s != "SECURITY" for s in [f.sensitivity_class, *(a.sensitivity_class for a in f.ancestors)])
        assert f.legal_basis_ref and f.tier2_agreement_valid
    if level >= 3:
        assert "PLATFORM_OPERATOR" in ok
    if level == 3:
        assert f.in_dataset_version
    if level == 4:
        assert f.privacy_status in {"NOT_APPLICABLE", "ANONYMIZED_VERIFIED"}
        assert all(s == "INTERNAL" for s in [f.sensitivity_class, *(a.sensitivity_class for a in f.ancestors)])


@settings(max_examples=1000, deadline=None)
@given(f=facts)
def test_t08_core_or_original_max_scope_is_l1(f: Facts) -> None:
    if f.variant == "ORIGINAL" or f.effective_role == "CORE":
        assert LEVEL[max_scope_for(f)] <= 1
