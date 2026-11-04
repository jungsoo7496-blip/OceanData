"""상태 전이 표 (domain.md 5장) — 표에 없는 전이는 거부 (속성 기반)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain import states
from app.domain.errors import InvalidStateTransition

INGEST_STATES = ["UPLOADING", "RECEIVED", "VALIDATING", "REGISTERED", "PROXIED", "READY_PRIVATE", "QUARANTINED",
                 "REJECTED", "FAILED"]
ROLES = ["ORG_ADMIN", "ORG_MEMBER", "DEVICE", "SERVICE", "PLATFORM_OPERATOR", "REVIEWER", "CONSUMER",
         "PROGRAM_VIEWER"]


def test_happy_paths() -> None:
    s = states.transition("ingest_status", None, "UPLOADING", {"DEVICE"})
    for nxt in ("RECEIVED",):
        s = states.transition("ingest_status", s, nxt, {"DEVICE"})
    for nxt in ("VALIDATING", "REGISTERED", "PROXIED", "READY_PRIVATE"):
        s = states.transition("ingest_status", s, nxt, {"SERVICE"})
    assert s == "READY_PRIVATE"


@pytest.mark.parametrize(("cur", "nxt", "role"), [
    ("UPLOADING", "REGISTERED", "SERVICE"),       # 검증을 건너뛸 수 없다
    ("RECEIVED", "VALIDATING", "ORG_ADMIN"),      # 검증 시작은 SERVICE만
    ("QUARANTINED", "REGISTERED", "ORG_ADMIN"),   # 격리 해제는 재검증으로만
    ("REJECTED", "VALIDATING", "ORG_ADMIN"),      # 폐기는 되돌릴 수 없다
    ("FAILED", "RECEIVED", "ORG_ADMIN"),          # 재처리는 운영자만
])
def test_forbidden_transitions(cur, nxt, role) -> None:
    with pytest.raises(InvalidStateTransition):
        states.transition("ingest_status", cur, nxt, {role})


@given(st.sampled_from([None, *INGEST_STATES]), st.sampled_from(INGEST_STATES), st.sets(st.sampled_from(ROLES)))
def test_only_table_entries_are_allowed(cur, nxt, roles) -> None:
    allowed = states.INGEST.get((cur, nxt), frozenset())
    assert states.can_transition("ingest_status", cur, nxt, roles) == bool(allowed & roles)


def test_share_and_grant_tables() -> None:
    assert states.can_transition("share_status", "NONE", "CANDIDATE", {"ORG_MEMBER"})
    assert not states.can_transition("share_status", "QUALITY_REVIEW", "APPROVED", {"ORG_ADMIN"})
    assert states.can_transition("share_status", "APPROVED", "NONE", {"ORG_ADMIN"})  # 철회
    assert not states.can_transition("grant_status", "REQUESTED", "GRANTED", {"PLATFORM_OPERATOR"})
