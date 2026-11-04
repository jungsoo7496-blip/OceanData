"""상태 전이 (domain.md 5장). 표에 없는 전이는 거부한다 (409 INVALID_STATE_TRANSITION).

주체: DEVICE(수집기·API 키), ORG(ORG_ADMIN·ORG_MEMBER), ORG_ADMIN, SERVICE, PLATFORM_OPERATOR, REVIEWER
"""

from __future__ import annotations

from app.domain.errors import InvalidStateTransition

ORG = frozenset({"ORG_ADMIN", "ORG_MEMBER"})
UPLOADER = ORG | {"DEVICE"}
PROCESSING = ("VALIDATING", "REGISTERED", "PROXIED")

# (현재, 다음) → 허용 주체. None = 상태 없음(생성)
INGEST: dict[tuple[str | None, str], frozenset[str]] = {
    (None, "UPLOADING"): UPLOADER,
    (None, "READY_PRIVATE"): UPLOADER,  # C0 메타데이터 전용 등록
    ("READY_PRIVATE", "UPLOADING"): UPLOADER,
    ("UPLOADING", "RECEIVED"): UPLOADER,
    ("RECEIVED", "VALIDATING"): frozenset({"SERVICE"}),
    ("VALIDATING", "REGISTERED"): frozenset({"SERVICE"}),
    ("VALIDATING", "QUARANTINED"): frozenset({"SERVICE"}),
    ("REGISTERED", "PROXIED"): frozenset({"SERVICE"}),
    ("REGISTERED", "READY_PRIVATE"): frozenset({"SERVICE"}),
    ("PROXIED", "READY_PRIVATE"): frozenset({"SERVICE"}),
    ("REGISTERED", "QUARANTINED"): frozenset({"SERVICE"}),
    ("PROXIED", "QUARANTINED"): frozenset({"SERVICE"}),
    ("QUARANTINED", "VALIDATING"): ORG,
    ("QUARANTINED", "REJECTED"): frozenset({"ORG_ADMIN", "SERVICE"}),
    **{(s, "FAILED"): frozenset({"SERVICE"}) for s in PROCESSING},
    **{("FAILED", s): frozenset({"PLATFORM_OPERATOR"}) for s in ("RECEIVED", *PROCESSING)},
}

SHARE: dict[tuple[str | None, str], frozenset[str]] = {
    ("NONE", "CANDIDATE"): ORG,
    ("CANDIDATE", "LEGAL_REVIEW"): frozenset({"PLATFORM_OPERATOR"}),
    ("LEGAL_REVIEW", "PROCESSING"): frozenset({"PLATFORM_OPERATOR"}),
    ("PROCESSING", "QUALITY_REVIEW"): frozenset({"SERVICE"}),
    ("QUALITY_REVIEW", "APPROVED"): frozenset({"PLATFORM_OPERATOR"}),
    ("QUALITY_REVIEW", "REPROCESS"): frozenset({"REVIEWER", "PLATFORM_OPERATOR"}),
    ("REPROCESS", "PROCESSING"): frozenset({"SERVICE"}),
    **{(s, "REJECTED"): frozenset({"PLATFORM_OPERATOR"}) for s in ("CANDIDATE", "LEGAL_REVIEW", "QUALITY_REVIEW")},
    ("REJECTED", "CANDIDATE"): frozenset({"ORG_ADMIN"}),
    **{(s, "NONE"): frozenset({"ORG_ADMIN"}) for s in ("CANDIDATE", "LEGAL_REVIEW", "PROCESSING", "QUALITY_REVIEW",
                                                         "APPROVED", "REJECTED", "REPROCESS")},
}

GRANT: dict[tuple[str | None, str], frozenset[str]] = {
    ("REQUESTED", "PROVIDER_APPROVED"): frozenset({"ORG_ADMIN"}),
    ("PROVIDER_APPROVED", "GRANTED"): frozenset({"PLATFORM_OPERATOR"}),
    ("REQUESTED", "DENIED"): frozenset({"ORG_ADMIN", "PLATFORM_OPERATOR", "SERVICE"}),
    ("PROVIDER_APPROVED", "DENIED"): frozenset({"ORG_ADMIN", "PLATFORM_OPERATOR", "SERVICE"}),
    ("GRANTED", "EXPIRED"): frozenset({"SERVICE"}),
    ("GRANTED", "REVOKED"): frozenset({"ORG_ADMIN", "PLATFORM_OPERATOR"}),
}

REPORT: dict[tuple[str | None, str], frozenset[str]] = {
    ("DRAFT", "APPROVED"): frozenset({"ORG_ADMIN"}),
    ("APPROVED", "SUBMITTED"): frozenset({"ORG_ADMIN"}),
    ("APPROVED", "SUPERSEDED"): frozenset({"SERVICE"}),
    ("SUBMITTED", "SUPERSEDED"): frozenset({"SERVICE"}),
}

TABLES = {"ingest_status": INGEST, "share_status": SHARE, "grant_status": GRANT, "report_status": REPORT}


def can_transition(field: str, current: str | None, target: str, actor_roles: frozenset[str] | set[str]) -> bool:
    allowed = TABLES[field].get((current, target))
    return allowed is not None and bool(allowed & set(actor_roles))


def transition(field: str, current: str | None, target: str, actor_roles: frozenset[str] | set[str]) -> str:
    if not can_transition(field, current, target, actor_roles):
        raise InvalidStateTransition(field, current, target, ",".join(sorted(actor_roles)))
    return target
