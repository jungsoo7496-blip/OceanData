"""수집 정상성 감시 (11번): 장비 상태 집계(Health_Monitor), 감시 규칙(Watch_Rule_Engine),
알림(Alert_Dispatcher), 장애 기록(Incident).

- 임계값은 watch_rules 표에 있다. 코드에는 규칙의 계산 방법만 둔다.
- 경보 상태는 규칙+대상 하나당 한 행(watch_alerts). 처음 위반 → 장애 기록 + 알림, 계속 위반 → 재알림 간격마다,
  해소 → 해제 알림 + 장애 자동 해결. 확인(ACKNOWLEDGED)한 장애는 재알림하지 않는다 (ADR-0017).
- 알림 본문에는 토큰·사전 서명 URL·개인정보를 넣지 않는다 (요구사항 3.4). 수신 주소는 받는 사람 목록에만 쓴다.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import and_, exists, func, or_, select, true
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import (
    AgentStatus,
    Asset,
    Device,
    DeviceCredential,
    DevicePolicy,
    HardwareSignalState,
    Incident,
    JobRow,
    OrgQuota,
    SessionDevice,
    SourceActivity,
    TrialSession,
    Upload,
    User,
    WatchAlert,
    WatchRule,
)
from app.domain.ids import new_id
from app.ports.notifier import Notification
from app.services.contracts import schema_files
from app.services.observations import OBS
from app.timefmt import iso

if TYPE_CHECKING:
    from app.container import Container

log = logging.getLogger("monitoring")

SEVERITY_RANK = {"OK": 0, "INFO": 1, "WARNING": 2, "CRITICAL": 3}

# 규칙 설명과 조치 안내 (요구사항 5.2: 기업 화면에 원인·조치 안내)
RULES: dict[str, tuple[str, str]] = {
    "HEARTBEAT_MISSING": ("수집기 하트비트 무응답",
                          "현장 PC가 켜져 있는지, marine-agent 서비스가 실행 중인지(marine-agent status), "
                          "인터넷 연결이 되는지 확인하세요. "
                          "문제가 계속되면 marine-agent diag 결과를 운영기관에 보내 주세요."),
    "INGEST_LAG": ("원천 장비 수집 지연",
                   "장비가 녹화·출력 중인지, 감시 폴더에 새 파일이 생기는지 확인하세요. "
                   "에이전트 대기열은 marine-agent status로 볼 수 있습니다."),
    "SESSION_NO_DATA": ("실증 예정일 무수집",
                        "회차에 지정된 장비가 실제로 수집 중인지, 폴더 이름이 회차 표시값과 같은지 확인하세요. "
                        "회차 미상으로 격리된 자산이 있으면 실증회차 화면에서 회차를 지정하세요."),
    "VOLUME_DROP": ("수집량 급감",
                    "평소보다 수집량이 크게 줄었습니다. 장비 설정·수집 정책 변경·장비 고장 여부를 확인하세요."),
    "UPLOAD_FAILURE_RATE": ("업로드 실패율 초과",
                            "업로드 실패가 늘었습니다. 회선 상태와 저장 한도를 확인하세요. "
                            "에이전트는 실패한 파트부터 다시 보냅니다."),
    "PROCESSING_BACKLOG": ("처리 대기 시간 초과",
                           "처리 대기가 길어졌습니다. 워커 상태와 GPU 적체를 확인하고 작업 우선순위를 조정하세요."),
    "QUARANTINE_SURGE": ("격리 건수 급증",
                         "격리가 급증했습니다. 수집 상태 화면의 실패 원인 상위 목록에서 격리 사유를 확인하세요."),
    "STORAGE_FORECAST": ("저장 용량 예측 경고",
                         "지금 추세면 저장 한도에 곧 도달합니다. 보존 정책을 점검하거나 한도 증설을 요청하세요."),
    "OPENAPI_FAILURE": ("외부 Open API 수집 실패",
                        "외부 Open API 수집이 연속 실패했습니다. 제공처 상태와 인증 정보를 확인하세요."),
    "OBSERVATION_LAG": ("관측값 수신 지연",
                        "관측값이 기대 주기보다 늦게 들어옵니다. 센서 전원, MQTT 브로커, 에이전트 연결을 확인하세요."),
    "CLOCK_OFFSET": ("시계 오차 초과",
                     "장비 시계가 서버와 어긋났습니다. 현장 PC·장비에 NTP 시간 동기화를 설정하세요. "
                     "시계가 틀리면 촬영 시각을 믿을 수 없습니다."),
    "LOCAL_DISK_LOW": ("현장 저장 여유 부족",
                       "현장 PC의 저장 공간이 부족합니다. 전송이 끝난 파일을 정리하거나 디스크를 늘려 주세요. "
                       "에이전트는 원본을 지우지 않습니다."),
    "HARDWARE_SIGNAL": ("하드웨어 경고",
                        "센터 하드웨어 경고입니다. 해당 장비의 디스크·온도·전원 상태를 점검하세요."),
}

# 규칙별 임계값 키와 형식 (PATCH 검사용)
PARAM_TYPES: dict[str, dict[str, type | tuple[str, ...]]] = {
    "HEARTBEAT_MISSING": {"factor": float, "min_silence_s": int},
    "INGEST_LAG": {"factor": float},
    "SESSION_NO_DATA": {"grace_s": int, "window_s": int},
    "VOLUME_DROP": {"baseline_days": int, "drop_pct": float, "min_baseline_daily": float},
    "UPLOAD_FAILURE_RATE": {"window_s": int, "max_failure_pct": float, "min_uploads": int},
    "PROCESSING_BACKLOG": {"max_wait_s": int},
    "QUARANTINE_SURGE": {"window_s": int, "min_count": int, "baseline_days": int, "factor": float},
    "STORAGE_FORECAST": {"days_left": float, "trend_days": int},
    "OPENAPI_FAILURE": {"consecutive_failures": int},
    "OBSERVATION_LAG": {"factor": float},
    "CLOCK_OFFSET": {"max_offset_ms": int, "window_s": int},
    "LOCAL_DISK_LOW": {"min_free_bytes": int},
    "HARDWARE_SIGNAL": {"min_status": ("WARNING", "CRITICAL"), "stale_s": int},
}


def validate_params(rule_code: str, params: dict[str, Any]) -> list[str]:
    """알 수 없는 키, 형식이 틀린 값, 0 이하 수치를 찾는다."""
    errors: list[str] = []
    spec = PARAM_TYPES[rule_code]
    for k, v in params.items():
        want = spec.get(k)
        if want is None:
            errors.append(f"알 수 없는 임계값: {k}")
        elif isinstance(want, tuple):
            if v not in want:
                errors.append(f"{k}: {', '.join(want)} 중 하나")
        elif isinstance(v, bool) or not isinstance(v, int | float) or (want is int and not isinstance(v, int)):
            errors.append(f"{k}: {'정수' if want is int else '숫자'}여야 합니다")
        elif v <= 0:
            errors.append(f"{k}: 0보다 커야 합니다")
    return errors


# ------------------------------------------------------------------ 장비 활동 갱신


def touch_registered(db: Session, organization_id: str, device_id: str, at: datetime) -> None:
    """자산이 정상 등록될 때 호출 (04·05번 등록 경로)."""
    stmt = insert(SourceActivity).values(device_id=device_id, organization_id=organization_id,
                                         last_registered_at=at, updated_at=at)
    db.execute(stmt.on_conflict_do_update(index_elements=["device_id"], set_={
        "last_registered_at": func.greatest(SourceActivity.last_registered_at, stmt.excluded.last_registered_at),
        "updated_at": stmt.excluded.updated_at}))


def touch_observations(db: Session, organization_id: str, device_id: str, last_observed: datetime,
                       received_at: datetime, clock_offset_ms: int | None) -> None:
    """관측값이 저장될 때 호출 (07번 수신 경로)."""
    stmt = insert(SourceActivity).values(device_id=device_id, organization_id=organization_id,
                                         last_observation_at=last_observed, last_observation_received_at=received_at,
                                         last_clock_offset_ms=clock_offset_ms, updated_at=received_at)
    db.execute(stmt.on_conflict_do_update(index_elements=["device_id"], set_={
        "last_observation_at": func.greatest(SourceActivity.last_observation_at, stmt.excluded.last_observation_at),
        "last_observation_received_at": stmt.excluded.last_observation_received_at,
        "last_clock_offset_ms": stmt.excluded.last_clock_offset_ms,
        "updated_at": stmt.excluded.updated_at}))


# ------------------------------------------------------------------ 상태 집계 (요구사항 1)


def _secs(now: datetime, t: datetime | None) -> int | None:
    return int((now - t).total_seconds()) if t else None


def latest_policies(db: Session, device_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not device_ids:
        return {}
    sub = select(DevicePolicy.device_id, func.max(DevicePolicy.version).label("v")) \
        .where(DevicePolicy.device_id.in_(device_ids)).group_by(DevicePolicy.device_id).subquery()
    rows = db.execute(select(DevicePolicy).join(sub, and_(DevicePolicy.device_id == sub.c.device_id,
                                                          DevicePolicy.version == sub.c.v))).scalars()
    return {r.device_id: r.policy for r in rows}


def policy_heartbeat_interval_s(policy: dict[str, Any]) -> int:
    """범위 밖인 이전 주기는 기본 정책의 60초를 사용한다 (ADR-0040)."""
    bounds = schema_files()["device-policy"]["properties"]["heartbeat_interval_s"]
    interval = policy.get("heartbeat_interval_s", 60)
    if isinstance(interval, (int, float)) and bounds["minimum"] <= interval <= bounds["maximum"]:
        if interval == int(interval):
            return int(interval)
    return 60


def rule_params(db: Session, code: str) -> dict[str, Any]:
    r = db.get(WatchRule, code)
    return dict(r.params) if r else {}


def heartbeat_threshold_s(params: dict[str, Any], interval_s: int) -> float:
    return max(float(params.get("factor", 3)) * interval_s, float(params.get("min_silence_s", 0)))


def device_health(db: Session, devices: list[Device], now: datetime) -> list[dict[str, Any]]:
    """장비별 상태 (AgentHealth 계약). 수집기는 하트비트, 원천 장비는 마지막 등록·관측 기준."""
    ids = [d.device_id for d in devices]
    if not ids:
        return []
    statuses = {s.collector_device_id: s for s in
                db.execute(select(AgentStatus).where(AgentStatus.collector_device_id.in_(ids))).scalars()}
    acts = {a.device_id: a for a in
            db.execute(select(SourceActivity).where(SourceActivity.device_id.in_(ids))).scalars()}
    # 수집기의 마지막 등록 = 자기와 연결된 원천 장비의 마지막 등록 중 가장 늦은 것
    by_collector: dict[str, datetime] = {}
    rows = db.execute(select(Device.collector_device_id, func.max(SourceActivity.last_registered_at))
                      .join(SourceActivity, SourceActivity.device_id == Device.device_id)
                      .where(Device.collector_device_id.in_(ids)).group_by(Device.collector_device_id)).all()
    for cid, t in rows:
        if t is not None:
            by_collector[cid] = t
    policies = latest_policies(db, [d.device_id for d in devices if d.kind == "COLLECTOR"])
    open_by_device: dict[str | None, int] = {k: int(n) for k, n in db.execute(
        select(Incident.device_id, func.count()).where(Incident.device_id.in_(ids), Incident.status != "RESOLVED")
        .group_by(Incident.device_id)).all()}
    hb_params = rule_params(db, "HEARTBEAT_MISSING")
    lag_factor = float(rule_params(db, "INGEST_LAG").get("factor", 3))
    out = []
    for d in devices:
        act = acts.get(d.device_id)
        base: dict[str, Any] = {
            "device_id": d.device_id, "organization_id": d.organization_id, "name": d.name, "kind": d.kind,
            "open_alerts": int(open_by_device.get(d.device_id, 0)), "expected_interval_s": d.expected_interval_s,
            "queue_items": 0, "queue_bytes": 0, "last_error_code": None, "last_heartbeat_at": None,
            "last_success_at": None, "policy_version_applied": None, "policy_version_current": "pv-0",
            "heartbeat_lag_s": None, "observation_lag_s": None, "clock_offset_ms": None, "policy_mismatch": False,
            "disk_free_bytes": None, "next_expected_at": None,
        }
        if d.kind == "COLLECTOR":
            st = statuses.get(d.device_id)
            pol = policies.get(d.device_id, {})
            current = str(pol.get("policy_version", "pv-0"))
            interval = policy_heartbeat_interval_s(pol)
            last_reg = max([t for t in (by_collector.get(d.device_id), act.last_registered_at if act else None)
                            if t is not None], default=None)
            hb = st.heartbeat if st else {}
            base.update({
                "policy_version_current": current, "last_registered_at": iso(last_reg), "last_observation_at": None,
                "lag_s": _secs(now, last_reg),
            })
            if st is None:
                status = "NEVER_SEEN"
            else:
                hb_lag = _secs(now, st.last_heartbeat_at) or 0
                base.update({
                    "last_heartbeat_at": iso(st.last_heartbeat_at), "last_success_at": iso(st.last_success_at),
                    "queue_items": st.queue_items, "queue_bytes": st.queue_bytes,
                    "last_error_code": st.last_error_code, "policy_version_applied": st.policy_version,
                    "heartbeat_lag_s": hb_lag, "clock_offset_ms": hb.get("clock_offset_ms"),
                    "policy_mismatch": st.policy_version != current, "disk_free_bytes": hb.get("disk_free_bytes"),
                    "next_expected_at": iso(st.last_heartbeat_at + timedelta(seconds=interval)),
                })
                if hb_lag > heartbeat_threshold_s(hb_params, interval):
                    status = "STALE"
                elif st.last_error_code:
                    status = "ERROR"
                else:
                    status = "OK"
        else:
            last_reg = act.last_registered_at if act else None
            last_obs = act.last_observation_at if act else None
            ref = last_obs or last_reg  # 관측값 장비는 마지막 관측 기준 (요구사항 1.3)
            base.update({
                "last_registered_at": iso(last_reg), "last_observation_at": iso(last_obs),
                "lag_s": _secs(now, ref), "observation_lag_s": _secs(now, last_obs),
                "clock_offset_ms": act.last_clock_offset_ms if act else None,
                "next_expected_at": iso(ref + timedelta(seconds=d.expected_interval_s))
                if ref and d.expected_interval_s else None,
            })
            if ref is None:
                status = "NEVER_SEEN"
            elif d.expected_interval_s and (now - ref).total_seconds() > lag_factor * d.expected_interval_s:
                status = "STALE"
            else:
                status = "OK"
        base["status"] = status
        out.append(base)
    return out


def top_failure_reasons(db: Session, now: datetime, org: str | None, limit: int = 10) -> list[dict[str, Any]]:
    """최근 24시간 실패 원인 상위 (요구사항 5.1)."""
    since = now - timedelta(hours=24)
    out: list[dict[str, Any]] = []

    def org_cond(col: Any) -> Any:
        return col == org if org else true()

    for code, n in db.execute(select(Asset.quarantine_reason, func.count()).where(
            Asset.ingest_status == "QUARANTINED", Asset.ingested_at >= since, Asset.quarantine_reason.is_not(None),
            org_cond(Asset.organization_id)).group_by(Asset.quarantine_reason)).all():
        out.append({"source": "QUARANTINE", "code": code, "count": int(n)})
    for code, n in db.execute(select(Upload.error_code, func.count()).where(
            Upload.error_code.is_not(None), Upload.updated_at >= since, org_cond(Upload.organization_id))
            .group_by(Upload.error_code)).all():
        out.append({"source": "UPLOAD", "code": code, "count": int(n)})
    for code, n in db.execute(select(JobRow.last_error_code, func.count()).where(
            JobRow.status == "FAILED", JobRow.updated_at >= since, JobRow.last_error_code.is_not(None),
            org_cond(JobRow.organization_id)).group_by(JobRow.last_error_code)).all():
        out.append({"source": "PROCESSING", "code": code, "count": int(n)})
    for code, n in db.execute(select(AgentStatus.last_error_code, func.count()).where(
            AgentStatus.last_error_code.is_not(None), org_cond(AgentStatus.organization_id))
            .group_by(AgentStatus.last_error_code)).all():
        out.append({"source": "AGENT", "code": code, "count": int(n)})
    return sorted(out, key=lambda x: (-x["count"], x["source"], x["code"]))[:limit]


# ------------------------------------------------------------------ 감시 규칙 (요구사항 2)


@dataclass
class Violation:
    rule_code: str
    target: str
    summary: str
    organization_id: str | None = None
    device_id: str | None = None
    session_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def alert_key(self) -> str:
        return f"{self.rule_code}:{self.target}"


@dataclass
class EvalCtx:
    db: Session
    now: datetime
    settings: Any
    _devices: list[Device] | None = None
    _health: dict[str, dict[str, Any]] | None = None

    def devices(self) -> list[Device]:
        if self._devices is None:
            self._devices = list(self.db.execute(select(Device).where(
                Device.status.not_in(["REVOKED", "RETIRED"]))).scalars())
        return self._devices

    def health(self) -> dict[str, dict[str, Any]]:
        if self._health is None:
            self._health = {h["device_id"]: h for h in device_health(self.db, self.devices(), self.now)}
        return self._health


Evaluator = Callable[[EvalCtx, dict[str, Any]], Iterable[Violation]]


def _heartbeat_missing(ctx: EvalCtx, p: dict[str, Any]) -> Iterable[Violation]:
    policies = latest_policies(ctx.db, [d.device_id for d in ctx.devices() if d.kind == "COLLECTOR"])
    for d in ctx.devices():
        if d.kind != "COLLECTOR" or d.status != "ACTIVE":
            continue  # 등록 전 수집기는 아직 보고할 수 없다
        interval = policy_heartbeat_interval_s(policies.get(d.device_id, {}))
        limit = heartbeat_threshold_s(p, interval)
        st = ctx.db.get(AgentStatus, d.device_id)
        last: datetime | None
        if st is not None:
            last = st.last_heartbeat_at
        else:  # 등록했는데 한 번도 보고하지 않음: 자격증명 발급 시각부터 잰다
            last = ctx.db.execute(select(func.max(DeviceCredential.created_at))
                                  .where(DeviceCredential.device_id == d.device_id)).scalar()
        if last is None:
            continue
        silence = (ctx.now - last).total_seconds()
        if silence > limit:
            yield Violation("HEARTBEAT_MISSING", d.device_id,
                            f"수집기 '{d.name}' 하트비트가 {int(silence // 60)}분째 없습니다",
                            d.organization_id, d.device_id,
                            detail={"silence_s": int(silence), "threshold_s": int(limit),
                                    "last_heartbeat_at": iso(last)})


def _ingest_lag(ctx: EvalCtx, p: dict[str, Any]) -> Iterable[Violation]:
    factor = float(p.get("factor", 3))
    for d in ctx.devices():
        h = ctx.health()[d.device_id]
        if d.kind != "SOURCE" or not d.expected_interval_s or h["last_observation_at"] or not h["last_registered_at"]:
            continue  # 관측값 장비는 OBSERVATION_LAG, 처음 붙는 장비는 첫 수신 전까지 보지 않는다
        if h["lag_s"] > factor * d.expected_interval_s:
            yield Violation("INGEST_LAG", d.device_id,
                            f"원천 장비 '{d.name}' 마지막 등록 후 {h['lag_s'] // 60}분 지났습니다 "
                            f"(기대 주기 {d.expected_interval_s // 60}분)",
                            d.organization_id, d.device_id,
                            detail={"lag_s": h["lag_s"], "expected_interval_s": d.expected_interval_s})


def _observation_lag(ctx: EvalCtx, p: dict[str, Any]) -> Iterable[Violation]:
    factor = float(p.get("factor", 3))
    for d in ctx.devices():
        h = ctx.health()[d.device_id]
        lag = h["observation_lag_s"]
        if d.kind != "SOURCE" or not d.expected_interval_s or lag is None:
            continue
        if lag > factor * d.expected_interval_s:
            yield Violation("OBSERVATION_LAG", d.device_id,
                            f"'{d.name}' 관측값이 {lag // 60}분째 들어오지 않습니다 "
                            f"(기대 주기 {d.expected_interval_s}초)",
                            d.organization_id, d.device_id,
                            detail={"observation_lag_s": lag, "expected_interval_s": d.expected_interval_s})


def _session_no_data(ctx: EvalCtx, p: dict[str, Any]) -> Iterable[Violation]:
    """시작 후 grace_s가 지난 회차에서, 지정 장비로부터 최근 window_s(긴 회차는 이 기간마다) 동안 유입이 0건."""
    grace, window = timedelta(seconds=int(p.get("grace_s", 7200))), timedelta(seconds=int(p.get("window_s", 172800)))
    sessions = ctx.db.execute(select(TrialSession).where(
        TrialSession.starts_at <= ctx.now - grace,
        or_(TrialSession.ends_at.is_(None), TrialSession.ends_at >= ctx.now - window))).scalars()
    for s in sessions:
        devices = list(ctx.db.execute(select(SessionDevice.device_id)
                                      .where(SessionDevice.session_id == s.session_id)).scalars())
        if not devices:
            continue  # 장비가 지정되지 않은 회차는 판단할 수 없다
        start = max(s.starts_at, ctx.now - window)
        end = min(s.ends_at or ctx.now, ctx.now)
        has_asset = ctx.db.execute(select(exists().where(
            Asset.organization_id == s.organization_id, Asset.ingested_at >= start,
            or_(Asset.session_id == s.session_id,
                and_(Asset.source_system_id.in_(devices), Asset.session_id.is_(None)))))).scalar()
        has_obs = ctx.db.execute(select(exists().where(
            OBS.c.organization_id == s.organization_id, OBS.c.source_system_id.in_(devices),
            OBS.c.observed_at >= start, OBS.c.observed_at <= end))).scalar()
        if not has_asset and not has_obs:
            hours = int((end - start).total_seconds() // 3600)
            yield Violation("SESSION_NO_DATA", s.session_id,
                            f"실증회차 '{s.label}' 진행 중인데 지정 장비 {len(devices)}대에서 "
                            f"최근 {hours}시간 받은 자료가 없습니다",
                            s.organization_id, session_id=s.session_id,
                            detail={"starts_at": iso(s.starts_at), "checked_from": iso(start),
                                    "device_count": len(devices)})


def _per_org_counts(ctx: EvalCtx, cond: Any, since: datetime, until: datetime | None = None) -> dict[str, int]:
    q = select(Asset.organization_id, func.count()).where(cond, Asset.ingested_at >= since)
    if until is not None:
        q = q.where(Asset.ingested_at < until)
    return {o: int(n) for o, n in ctx.db.execute(q.group_by(Asset.organization_id)).all()}


def _volume_drop(ctx: EvalCtx, p: dict[str, Any]) -> Iterable[Violation]:
    days = int(p.get("baseline_days", 7))
    day_start = ctx.now - timedelta(days=1)
    originals = and_(Asset.variant == "ORIGINAL", Asset.deleted_at.is_(None))
    recent = _per_org_counts(ctx, originals, day_start)
    base = _per_org_counts(ctx, originals, day_start - timedelta(days=days), day_start)
    for org, total in base.items():
        avg = total / days
        if avg < float(p.get("min_baseline_daily", 10)):
            continue
        got = recent.get(org, 0)
        if got < avg * float(p.get("drop_pct", 50)) / 100:
            yield Violation("VOLUME_DROP", org,
                            f"최근 24시간 수집 {got}건 — 평소 하루 평균 {avg:.0f}건보다 크게 적습니다",
                            org, detail={"last_24h": got, "baseline_daily": round(avg, 1)})


def _upload_failure_rate(ctx: EvalCtx, p: dict[str, Any]) -> Iterable[Violation]:
    since = ctx.now - timedelta(seconds=int(p.get("window_s", 3600)))
    failed = or_(Upload.status == "EXPIRED", Upload.error_code.is_not(None))
    rows = ctx.db.execute(select(Upload.organization_id, func.count(), func.count().filter(failed))
                          .where(Upload.created_at >= since).group_by(Upload.organization_id)).all()
    for org, total, bad in rows:
        if total < int(p.get("min_uploads", 5)):
            continue
        pct = 100 * bad / total
        if pct > float(p.get("max_failure_pct", 20)):
            yield Violation("UPLOAD_FAILURE_RATE", org, f"업로드 {total}건 중 {bad}건 실패 ({pct:.0f}%)", org,
                            detail={"uploads": int(total), "failed": int(bad), "failure_pct": round(pct, 1)})


def _processing_backlog(ctx: EvalCtx, p: dict[str, Any]) -> Iterable[Violation]:
    limit = ctx.now - timedelta(seconds=int(p.get("max_wait_s", 3600)))
    rows = ctx.db.execute(select(JobRow.job_type, func.count(), func.min(JobRow.run_after)).where(
        JobRow.status == "QUEUED", JobRow.run_after <= limit).group_by(JobRow.job_type)).all()
    for job_type, n, oldest in rows:
        yield Violation("PROCESSING_BACKLOG", job_type,
                        f"'{job_type}' 작업 {n}건이 "
                        f"{int((ctx.now - oldest).total_seconds() // 60)}분 넘게 대기 중입니다",
                        detail={"job_type": job_type, "waiting": int(n), "oldest_run_after": iso(oldest)})


def _quarantine_surge(ctx: EvalCtx, p: dict[str, Any]) -> Iterable[Violation]:
    window = timedelta(seconds=int(p.get("window_s", 3600)))
    days = int(p.get("baseline_days", 7))
    quarantined = Asset.ingest_status == "QUARANTINED"
    recent = _per_org_counts(ctx, quarantined, ctx.now - window)
    base = _per_org_counts(ctx, quarantined, ctx.now - timedelta(days=days), ctx.now - window)
    windows = max((timedelta(days=days) - window) / window, 1)
    for org, n in recent.items():
        expected = base.get(org, 0) / windows
        if n >= int(p.get("min_count", 10)) and n > float(p.get("factor", 3)) * expected:
            yield Violation("QUARANTINE_SURGE", org,
                            f"최근 {int(window.total_seconds() // 60)}분 격리 {n}건 (평소 {expected:.1f}건)",
                            org, detail={"recent": n, "baseline_per_window": round(expected, 2)})


def _storage_forecast(ctx: EvalCtx, p: dict[str, Any]) -> Iterable[Violation]:
    days = int(p.get("trend_days", 7))
    growth: dict[str, int] = dict(ctx.db.execute(  # type: ignore[arg-type]
        select(Asset.organization_id, func.coalesce(func.sum(Asset.byte_size), 0)).where(
        Asset.ingested_at >= ctx.now - timedelta(days=days)).group_by(Asset.organization_id)).all())
    for q in ctx.db.execute(select(OrgQuota)).scalars():
        if not q.quota_bytes:
            continue
        remaining = q.quota_bytes - (q.used_bytes + q.quarantine_bytes + q.reserved_bytes + q.backup_bytes)
        daily = int(growth.get(q.organization_id, 0)) / days
        days_left = remaining / daily if daily > 0 else None
        if remaining <= 0 or (days_left is not None and days_left < float(p.get("days_left", 30))):
            left = "이미 초과" if remaining <= 0 else f"약 {days_left:.0f}일 뒤"
            yield Violation("STORAGE_FORECAST", q.organization_id, f"지금 추세면 저장 한도에 {left} 도달합니다",
                            q.organization_id,
                            detail={"remaining_bytes": int(remaining), "daily_growth_bytes": int(daily),
                                    "days_left": None if days_left is None else round(days_left, 1)})


def _clock_offset(ctx: EvalCtx, p: dict[str, Any]) -> Iterable[Violation]:
    limit = int(p.get("max_offset_ms", ctx.settings.clock_offset_warn_ms))
    recent = ctx.now - timedelta(seconds=int(p.get("window_s", 3600)))
    for d in ctx.devices():
        h = ctx.health()[d.device_id]
        off = h["clock_offset_ms"]
        if off is None or abs(int(off)) <= limit:
            continue
        if d.kind == "SOURCE":
            act = ctx.db.get(SourceActivity, d.device_id)
            if act is None or act.last_observation_received_at is None or act.last_observation_received_at < recent:
                continue  # 오래된 측정값으로 경보하지 않는다
        yield Violation("CLOCK_OFFSET", d.device_id,
                        f"'{d.name}' 시계가 서버와 {abs(int(off)) / 1000:.1f}초 어긋났습니다",
                        d.organization_id, d.device_id, detail={"clock_offset_ms": int(off), "threshold_ms": limit})


def _local_disk_low(ctx: EvalCtx, p: dict[str, Any]) -> Iterable[Violation]:
    limit = int(p.get("min_free_bytes", 10 * 1024**3))
    for d in ctx.devices():
        free = ctx.health()[d.device_id]["disk_free_bytes"]
        if d.kind == "COLLECTOR" and free is not None and free < limit:
            yield Violation("LOCAL_DISK_LOW", d.device_id,
                            f"'{d.name}' 저장 여유 {free / 1024**3:.1f}GB (기준 {limit / 1024**3:.0f}GB)",
                            d.organization_id, d.device_id,
                            detail={"disk_free_bytes": int(free), "threshold_bytes": limit})


def _hardware_signal(ctx: EvalCtx, p: dict[str, Any]) -> Iterable[Violation]:
    floor = SEVERITY_RANK[str(p.get("min_status", "WARNING"))]
    fresh = ctx.now - timedelta(seconds=int(p.get("stale_s", 900)))
    for s in ctx.db.execute(select(HardwareSignalState).where(HardwareSignalState.observed_at >= fresh)).scalars():
        if SEVERITY_RANK.get(s.status, 0) >= floor:
            what = f"{s.host} {s.kind}" + (f" {s.component}" if s.component else "")
            val = "" if s.value is None else f" ({s.value:g}{s.unit or ''})"
            yield Violation("HARDWARE_SIGNAL", f"{s.host}/{s.kind}/{s.component}", f"{what} {s.status}{val}",
                            detail={"host": s.host, "kind": s.kind, "component": s.component, "status": s.status,
                                    "value": s.value, "unit": s.unit, "observed_at": iso(s.observed_at)})


def _openapi_failure(ctx: EvalCtx, p: dict[str, Any]) -> Iterable[Violation]:
    """12번 Open API 수집: 연속 실패 또는 응답 형식 변경 (12번 요구사항 3.1)."""
    from app.db.models import ExternalSource

    limit = int(p.get("consecutive_failures", 3))
    for s in ctx.db.execute(select(ExternalSource).where(ExternalSource.enabled)).scalars():
        if s.format_error_at is not None or s.consecutive_failures >= limit:
            why = ("응답 형식이 필드 매핑과 다릅니다 (원 응답 격리)" if s.format_error_at
                   else f"연속 {s.consecutive_failures}회 실패")
            yield Violation("OPENAPI_FAILURE", s.source_id, f"외부 소스 '{s.name}': {why}",
                            detail={"source_id": s.source_id, "consecutive_failures": s.consecutive_failures,
                                    "last_error": s.last_error})


EVALUATORS: dict[str, Evaluator] = {
    "OPENAPI_FAILURE": _openapi_failure,
    "HEARTBEAT_MISSING": _heartbeat_missing,
    "INGEST_LAG": _ingest_lag,
    "SESSION_NO_DATA": _session_no_data,
    "VOLUME_DROP": _volume_drop,
    "UPLOAD_FAILURE_RATE": _upload_failure_rate,
    "PROCESSING_BACKLOG": _processing_backlog,
    "QUARANTINE_SURGE": _quarantine_surge,
    "STORAGE_FORECAST": _storage_forecast,
    "OBSERVATION_LAG": _observation_lag,
    "CLOCK_OFFSET": _clock_offset,
    "LOCAL_DISK_LOW": _local_disk_low,
    "HARDWARE_SIGNAL": _hardware_signal,
}


# ------------------------------------------------------------------ 알림 (요구사항 3)


def recipients(db: Session, rule: WatchRule, organization_id: str | None) -> tuple[str, ...]:
    conds = []
    if rule.notify_org and organization_id:
        conds.append(and_(User.organization_id == organization_id, User.roles.any("ORG_ADMIN")))  # type: ignore[arg-type]
    if rule.notify_operator:
        conds.append(User.roles.any("PLATFORM_OPERATOR"))  # type: ignore[arg-type]
    if not conds:
        return ()
    rows = db.execute(select(User.contact_email).where(User.status == "ACTIVE", User.contact_email.is_not(None),
                                                       or_(*conds))).scalars()
    return tuple(sorted({r for r in rows if r}))


def post_webhook(url: str, secret: str | None, payload: dict[str, Any], now: datetime) -> None:
    """운영자 알림 웹훅. 본문은 코드·식별자·요약만 (토큰·URL·개인정보 없음)."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "X-Signature-Timestamp": str(int(now.timestamp()))}
    if secret:
        mac = hmac.new(secret.encode(), headers["X-Signature-Timestamp"].encode() + b"." + body, hashlib.sha256)
        headers["X-Signature"] = "sha256=" + mac.hexdigest()
    try:
        httpx.post(url, content=body, headers=headers, timeout=5.0).raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("alert_webhook_failed", extra={"error_type": type(exc).__name__})


def dispatch(c: Container, db: Session, rule: WatchRule, incident: Incident, kind: str, now: datetime) -> None:
    """kind: OPENED / REMINDER / RESOLVED."""
    if kind in ("OPENED", "RESOLVED") and rule.notify_org and incident.organization_id:
        # 기관 웹훅 구독 (11번 요구사항 3.1, 04번 웹훅): 열림·해제만 (재알림은 보내지 않는다)
        from app.services import webhooks

        webhooks.emit(db, incident.organization_id, f"incident.{kind.lower()}", {
            "incident_id": incident.incident_id, "rule_code": rule.rule_code, "severity": incident.severity,
            "device_id": incident.device_id, "session_id": incident.session_id})
    label = {"OPENED": "경보", "REMINDER": "경보 계속", "RESOLVED": "해제"}[kind]
    title = RULES[rule.rule_code][0]
    subject = f"[{label}] {title} ({incident.severity})"
    lines = [incident.summary, "", f"장애 번호: {incident.incident_id}", f"발생: {iso(incident.opened_at)} (UTC)"]
    if kind == "RESOLVED":
        lines.append(f"해제: {iso(now)} (UTC)")
    else:
        lines += ["", "조치 안내:", incident.guidance]
    lines += ["", "플랫폼의 수집 상태 화면에서 자세한 내용을 확인하세요."]
    body = "\n".join(lines)
    if "EMAIL" in rule.channels:
        c.notifier.send(Notification(kind=f"watch.{kind.lower()}", organization_id=incident.organization_id,
                                     subject=subject, body=body,
                                     recipients=recipients(db, rule, incident.organization_id),
                                     data={"incident_id": incident.incident_id, "rule_code": rule.rule_code,
                                           "severity": incident.severity, "device_id": incident.device_id,
                                           "session_id": incident.session_id}))
    url = c.settings.alert_webhook_url
    if "WEBHOOK" in rule.channels and rule.notify_operator and url:
        secret = c.settings.alert_webhook_secret.get_secret_value() if c.settings.alert_webhook_secret else None
        post_webhook(url, secret, {
            "type": f"watch.{kind.lower()}", "incident_id": incident.incident_id, "rule_code": rule.rule_code,
            "severity": incident.severity, "organization_id": incident.organization_id,
            "device_id": incident.device_id, "session_id": incident.session_id, "summary": incident.summary,
            "occurred_at": iso(now)}, now)


# ------------------------------------------------------------------ 평가·장애 기록 (요구사항 3, 4)


@dataclass
class EvalResult:
    opened: list[str] = field(default_factory=list)
    opened_rules: list[str] = field(default_factory=list)
    reminded: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    active_by_rule: dict[str, int] = field(default_factory=dict)
    health: list[dict[str, Any]] = field(default_factory=list)


def evaluate(c: Container, db: Session, now: datetime | None = None) -> EvalResult:
    """모든 활성 규칙을 평가하고 경보·장애·알림을 갱신한다. 워커 정비 주기에서 부른다."""
    now = now or c.clock.now()
    ctx = EvalCtx(db, now, c.settings)
    rules = {r.rule_code: r for r in db.execute(select(WatchRule)).scalars()}
    found: dict[str, tuple[WatchRule, Violation]] = {}
    for code, fn in EVALUATORS.items():
        rule = rules.get(code)
        if rule is None or not rule.enabled:
            continue
        for v in fn(ctx, dict(rule.params)):
            found[v.alert_key] = (rule, v)
    res = EvalResult()
    active = {a.alert_key: a for a in db.execute(select(WatchAlert).where(WatchAlert.active)
                                                 .with_for_update()).scalars()}
    for key, (rule, v) in found.items():
        alert = active.get(key) or db.get(WatchAlert, key)
        if alert is None or not alert.active:
            title, guidance = RULES[rule.rule_code]
            inc = Incident(incident_id=new_id("incident"), alert_key=key, rule_code=rule.rule_code,
                           severity=rule.severity, status="OPEN", organization_id=v.organization_id,
                           device_id=v.device_id, session_id=v.session_id, summary=v.summary, guidance=guidance,
                           opened_at=now)
            db.add(inc)
            values = {"rule_code": rule.rule_code, "organization_id": v.organization_id, "device_id": v.device_id,
                      "session_id": v.session_id, "active": True, "first_seen_at": now, "last_seen_at": now,
                      "last_notified_at": now, "cleared_at": None, "incident_id": inc.incident_id,
                      "detail": v.detail}
            if alert is None:
                db.add(WatchAlert(alert_key=key, **values))
            else:
                for k, val in values.items():
                    setattr(alert, k, val)
            db.flush()
            dispatch(c, db, rule, inc, "OPENED", now)
            res.opened.append(inc.incident_id)
            res.opened_rules.append(rule.rule_code)
            continue
        alert.last_seen_at, alert.detail = now, v.detail
        inc_row = db.get(Incident, alert.incident_id) if alert.incident_id else None
        due = alert.last_notified_at is None or \
            (now - alert.last_notified_at).total_seconds() >= rule.realert_interval_s
        if inc_row is not None and inc_row.status == "OPEN" and due:
            inc_row.summary = v.summary  # 최신 상황으로
            dispatch(c, db, rule, inc_row, "REMINDER", now)
            alert.last_notified_at = now
            res.reminded.append(inc_row.incident_id)
    for key, alert in active.items():
        if key in found:
            continue
        alert.active, alert.cleared_at = False, now
        inc_row = db.get(Incident, alert.incident_id) if alert.incident_id else None
        rule_row = rules.get(alert.rule_code)
        disabled = rule_row is None or not rule_row.enabled
        if inc_row is not None and inc_row.status != "RESOLVED":
            note = "규칙이 비활성화되어 종료" if disabled else "위반 해소로 자동 해제"
            inc_row.status, inc_row.resolved_at, inc_row.resolved_by = "RESOLVED", now, "system"
            inc_row.memo = f"{inc_row.memo}\n{note}" if inc_row.memo else note
        if inc_row is not None and rule_row is not None and not disabled:
            dispatch(c, db, rule_row, inc_row, "RESOLVED", now)
            res.resolved.append(inc_row.incident_id)
    for _, (rule, _v) in found.items():
        res.active_by_rule[rule.rule_code] = res.active_by_rule.get(rule.rule_code, 0) + 1
    res.health = list(ctx.health().values())
    return res


def incident_dict(i: Incident) -> dict[str, Any]:
    return {"incident_id": i.incident_id, "organization_id": i.organization_id, "device_id": i.device_id,
            "session_id": i.session_id, "rule_code": i.rule_code, "severity": i.severity, "status": i.status,
            "summary": i.summary, "guidance": i.guidance, "opened_at": iso(i.opened_at),
            "acknowledged_at": iso(i.acknowledged_at), "resolved_at": iso(i.resolved_at), "assignee": i.assignee,
            "memo": i.memo}


def rule_dict(r: WatchRule) -> dict[str, Any]:
    return {"rule_code": r.rule_code, "description": RULES[r.rule_code][0], "enabled": r.enabled,
            "severity": r.severity, "params": r.params, "notify_org": r.notify_org,
            "notify_operator": r.notify_operator, "channels": r.channels, "realert_interval_s": r.realert_interval_s,
            "data_source_ready": r.rule_code in EVALUATORS, "updated_at": iso(r.updated_at)}
