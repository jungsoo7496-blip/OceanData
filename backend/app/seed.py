"""시험 데이터 (01번 요구사항 6). 고정 ID라 여러 번 실행해도 중복이 생기지 않는다.

계정은 deploy/compose/keycloak/realm-marine.json의 사용자와 idp_subject로 연결된다.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import Device, Organization, Project, ProjectAssignment, SessionDevice, TrialSession, User


def fid(prefix: str, n: int) -> str:
    return f"{prefix}_{n:026d}"


def sub(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


ORG_A, ORG_B, ORG_AGENCY, ORG_OPERATOR, ORG_CONSUMER = (fid("org", i) for i in (1, 2, 3, 4, 5))
PRJ_A, PRJ_B = fid("prj", 201), fid("prj", 202)
DEV_A_COLLECTOR, DEV_A_CAMERA, DEV_A_SENSOR = fid("dev", 301), fid("dev", 302), fid("dev", 303)
DEV_B_COLLECTOR, DEV_B_CAMERA = fid("dev", 311), fid("dev", 312)
SES_A, SES_B = fid("ses", 401), fid("ses", 411)

ORGS = [
    (ORG_A, "제공기관 A (시험)", "PROVIDER"),
    (ORG_B, "제공기관 B (시험)", "PROVIDER"),
    (ORG_AGENCY, "관리기관 (시험)", "PROGRAM_AGENCY"),
    (ORG_OPERATOR, "운영기관 (시험)", "OPERATOR"),
    (ORG_CONSUMER, "이용기관 (시험)", "CONSUMER"),
]
# (user_id, org, username, roles, idp_subject)
USERS = [
    (fid("usr", 101), ORG_A, "admin-a", ["ORG_ADMIN"], sub(101)),
    (fid("usr", 102), ORG_A, "member-a", ["ORG_MEMBER"], sub(102)),
    (fid("usr", 103), ORG_B, "admin-b", ["ORG_ADMIN"], sub(103)),
    (fid("usr", 104), ORG_B, "member-b", ["ORG_MEMBER"], sub(104)),
    (fid("usr", 105), ORG_AGENCY, "viewer", ["PROGRAM_VIEWER"], sub(105)),
    (fid("usr", 106), ORG_OPERATOR, "operator", ["PLATFORM_OPERATOR"], sub(106)),
    (fid("usr", 107), ORG_OPERATOR, "operator2", ["PLATFORM_OPERATOR"], sub(107)),
    (fid("usr", 108), ORG_OPERATOR, "reviewer", ["REVIEWER"], sub(108)),
    (fid("usr", 109), ORG_CONSUMER, "consumer", ["CONSUMER"], sub(109)),
]
SEED_AT = datetime(2026, 11, 1, tzinfo=UTC)


def _upsert(db: Session, model: type, rows: list[dict], keys: list[str]) -> None:
    if rows:
        db.execute(insert(model).values(rows).on_conflict_do_nothing(index_elements=keys))


def seed(db: Session) -> None:
    t = SEED_AT
    _upsert(db, Organization, [{"organization_id": o, "name": n, "org_type": ty, "status": "ACTIVE",
                                "created_at": t} for o, n, ty in ORGS], ["organization_id"])
    _upsert(db, User, [{"user_id": u, "organization_id": o, "username": n, "roles": r, "idp_subject": s,
                        "status": "ACTIVE", "created_at": t, "updated_at": t} for u, o, n, r, s in USERS],
            ["user_id"])
    _upsert(db, Project, [
        {"project_id": PRJ_A, "organization_id": ORG_A, "name": "A 자율운항 선박 실증", "status": "ACTIVE",
         "created_at": t},
        {"project_id": PRJ_B, "organization_id": ORG_B, "name": "B 해양 센서 실증", "status": "ACTIVE",
         "created_at": t},
    ], ["project_id"])
    _upsert(db, ProjectAssignment, [{"project_id": PRJ_A, "user_id": fid("usr", 105), "created_at": t}],
            ["project_id", "user_id"])
    dev = {"local_retention_days": None, "expected_interval_s": None, "source_timezone": None,
           "last_heartbeat_at": None, "created_at": t}
    _upsert(db, Device, [
        {**dev, "device_id": DEV_A_COLLECTOR, "organization_id": ORG_A, "project_id": PRJ_A, "name": "A 선박 PC",
         "kind": "COLLECTOR", "device_type": "AGENT_PC", "collector_device_id": None, "status": "PENDING_ENROLLMENT",
         "collection_mode": "C3_APPROVED_FULL", "local_copy_policy": "RETAINED"},
        {**dev, "device_id": DEV_A_CAMERA, "organization_id": ORG_A, "project_id": PRJ_A, "name": "A 선수 카메라",
         "kind": "SOURCE", "device_type": "CAMERA", "collector_device_id": DEV_A_COLLECTOR, "status": "ACTIVE",
         "collection_mode": "C2_EVENT_CLIPS", "local_copy_policy": "ROTATING"},
        {**dev, "device_id": DEV_A_SENSOR, "organization_id": ORG_A, "project_id": PRJ_A, "name": "A 수질 센서",
         "kind": "SOURCE", "device_type": "SENSOR", "collector_device_id": DEV_A_COLLECTOR, "status": "ACTIVE",
         "collection_mode": "C3_APPROVED_FULL", "local_copy_policy": "RETAINED", "source_timezone": "Asia/Seoul"},
        {**dev, "device_id": DEV_B_COLLECTOR, "organization_id": ORG_B, "project_id": PRJ_B, "name": "B 엣지 박스",
         "kind": "COLLECTOR", "device_type": "EDGE_BOX", "collector_device_id": None, "status": "PENDING_ENROLLMENT",
         "collection_mode": "C1_SAMPLE_PREVIEW", "local_copy_policy": "UNKNOWN"},
        {**dev, "device_id": DEV_B_CAMERA, "organization_id": ORG_B, "project_id": PRJ_B, "name": "B CCTV",
         "kind": "SOURCE", "device_type": "CAMERA", "collector_device_id": DEV_B_COLLECTOR, "status": "ACTIVE",
         "collection_mode": "C1_SAMPLE_PREVIEW", "local_copy_policy": "ROTATING"},
    ], ["device_id"])
    _upsert(db, TrialSession, [
        {"session_id": SES_A, "organization_id": ORG_A, "project_id": PRJ_A, "label": "A-2026-11-01",
         "title": "A 1차 항해 시험", "starts_at": t, "ends_at": None, "site": "시험 해역 1", "note": None,
         "created_at": t},
        {"session_id": SES_B, "organization_id": ORG_B, "project_id": PRJ_B, "label": "B-2026-11-01",
         "title": "B 센서 시험", "starts_at": t, "ends_at": None, "site": "시험 해역 2", "note": None,
         "created_at": t},
    ], ["session_id"])
    _upsert(db, SessionDevice, [{"session_id": SES_A, "device_id": DEV_A_CAMERA},
                                {"session_id": SES_A, "device_id": DEV_A_SENSOR},
                                {"session_id": SES_B, "device_id": DEV_B_CAMERA}], ["session_id", "device_id"])
