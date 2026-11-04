"""Sharing_Rules — domain.md 4장 공유 가능 조건을 이 한 곳에만 구현한다 (13번 요구사항 3).

순수 함수다(프레임워크·DB 없음). 서비스는 자산과 조상(계보)의 사실을 모아 `Facts`로 넘기고, 여기서 나온
미충족 조건 목록이 비어 있을 때만 sharing_scope를 바꾼다. 하향은 조건 없이 언제든 (ORG_ADMIN).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from app.domain.errors import UnmetCondition

SCOPES = ("PRIVATE", "PROGRAM_REVIEW", "SANDBOX_PARTNERS", "APPROVED_PARTNERS", "PUBLIC")
LEVEL = {s: i for i, s in enumerate(SCOPES)}  # L0~L4
LABEL = {s: f"L{i}" for i, s in enumerate(SCOPES)}
SENSITIVITY_ORDER = {"INTERNAL": 0, "RESTRICTED": 1, "SECURITY": 2}
L2_PRIVACY = frozenset({"NOT_APPLICABLE", "PSEUDONYMIZED", "ANONYMIZED_VERIFIED"})
L4_PRIVACY = frozenset({"NOT_APPLICABLE", "ANONYMIZED_VERIFIED"})


@dataclass(frozen=True)
class Lineal:
    """자산 하나의 공유 판단용 속성."""

    variant: str
    data_role: str
    sensitivity_class: str


@dataclass(frozen=True)
class Facts:
    """판단 대상 자산과 그 조상(계보 위쪽 전부)의 사실."""

    variant: str
    data_role: str
    sensitivity_class: str
    privacy_status: str
    review_result: str | None
    share_status: str
    legal_basis_ref: str | None
    tier2_agreement_valid: bool
    in_dataset_version: bool = False
    ancestors: tuple[Lineal, ...] = field(default_factory=tuple)

    @property
    def effective_role(self) -> str:
        """파생본은 원 자산들의 가장 엄격한 data_role을 물려받는다 (요구사항 3.6)."""
        roles = [self.data_role, *(a.data_role for a in self.ancestors)]
        return "CORE" if "CORE" in roles else "BYPRODUCT"

    @property
    def effective_sensitivity(self) -> str:
        values = [self.sensitivity_class, *(a.sensitivity_class for a in self.ancestors)]
        return max(values, key=lambda v: SENSITIVITY_ORDER.get(v, 2))  # 모르는 값은 가장 엄격하게

    @property
    def derived_from_core_original(self) -> bool:
        return any(a.variant == "ORIGINAL" and a.data_role == "CORE" for a in self.ancestors)


def evaluate(target: str, f: Facts, approvals: Iterable[str]) -> list[UnmetCondition]:
    """목표 등급(상향)의 조건 중 충족하지 못한 것. approvals는 이 결정에 참여한 승인 주체 역할."""
    ok = set(approvals)
    unmet: list[UnmetCondition] = []

    def need(cond: bool, condition: str, actual: object) -> None:
        if not cond:
            unmet.append(UnmetCondition(condition=condition, actual=None if actual is None else str(actual)))

    level = LEVEL[target]
    if level == 0:
        return []
    need("ORG_ADMIN" in ok, "ORG_ADMIN 승인", "없음")
    if level == 1:
        return unmet
    need(f.variant != "ORIGINAL", "variant ≠ ORIGINAL", f.variant)
    need(f.effective_role == "BYPRODUCT", "data_role=BYPRODUCT (원 자산 포함)", f.effective_role)
    need(not f.derived_from_core_original, "CORE 원본의 파생본이 아님", "CORE 원본에서 파생")
    need(f.privacy_status in L2_PRIVACY, "privacy_status ∈ {NOT_APPLICABLE, PSEUDONYMIZED, ANONYMIZED_VERIFIED}",
         f.privacy_status)
    need(f.review_result == "PASSED", "review_result=PASSED", f.review_result)
    need(f.share_status == "APPROVED", "share_status=APPROVED (운영자 최종 승인)", f.share_status)
    need(f.effective_sensitivity != "SECURITY", "sensitivity_class ≠ SECURITY", f.effective_sensitivity)
    need(bool(f.legal_basis_ref), "legal_basis_ref 존재", f.legal_basis_ref)
    need(f.tier2_agreement_valid, "제공기관 2단 약정 유효", "없음 또는 만료")
    if level == 3:
        need(f.in_dataset_version, "데이터셋 버전에 포함", "포함 안 됨")
        need("PLATFORM_OPERATOR" in ok, "PLATFORM_OPERATOR 승인", "없음")
    if level == 4:
        need(f.privacy_status in L4_PRIVACY, "privacy_status ∈ {NOT_APPLICABLE, ANONYMIZED_VERIFIED}", f.privacy_status)
        need(f.effective_sensitivity == "INTERNAL", "sensitivity_class=INTERNAL", f.effective_sensitivity)
        need("PLATFORM_OPERATOR" in ok, "PLATFORM_OPERATOR 승인", "없음")
    return unmet


def pending_approvals(target: str) -> frozenset[str]:
    """목표 등급에 필요한 승인 주체 (요청자 외에 더 받아야 할 수 있는 것)."""
    level = LEVEL[target]
    if level >= 3:
        return frozenset({"ORG_ADMIN", "PLATFORM_OPERATOR"})
    return frozenset({"ORG_ADMIN"}) if level >= 1 else frozenset()


def max_scope_for(f: Facts) -> str:
    """모든 승인이 있다고 가정할 때 올릴 수 있는 가장 높은 등급 (화면 안내·시험용)."""
    best = "PRIVATE"
    for s in SCOPES[1:]:
        if not evaluate(s, f, {"ORG_ADMIN", "PLATFORM_OPERATOR"}):
            best = s
    return best


def dataset_scope(item_scopes: Iterable[str]) -> str:
    """데이터셋 버전의 등급은 항목 중 가장 낮은 등급 (domain.md 4장)."""
    scopes = list(item_scopes)
    return min(scopes, key=lambda s: LEVEL[s]) if scopes else "PRIVATE"


def is_downgrade(current: str, target: str) -> bool:
    return LEVEL[target] < LEVEL[current]
