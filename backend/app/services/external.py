"""공공 Open API 수집·환경 정보 결합 (12번).

- Source_Registry(`external_sources`): 어댑터 종류·엔드포인트·인증키 참조(비밀값 아님)·주기·관측소·필드 매핑·출처 표기·
  재배포 허용·과거 조회 지원·호출 한도. 명세가 확인되지 않은 소스는 MOCK 어댑터로만 (spec_status=NEEDS_CONFIRMATION).
- Collection_Scheduler: 워커 정비 주기에 소스별 주기가 되면 `collect_source` 작업, 실패는 큐의 지수 백오프.
  수집 기준점(collected_until) 뒤 빈 구간은 과거 조회를 지원하면 보충, 한 번에 `max_window`까지, 호출 한도 안에서.
- 응답 형식이 매핑과 맞지 않으면 원 응답을 격리 버킷에 보관하고 실패를 센다 → 11번 OPENAPI_FAILURE 경보.
- 관측소마다 원천 장비(EXTERNAL_STATION, 운영기관 소유)를 두고 07번 관측값 저장소에 넣는다 (단위 정규화·원래 값 보존).
- Env_Joiner: 촬영 구간·위치가 있는 자산에 반경·시간 창 안 가장 가까운 관측값을 env_context로 (거리·시각 차이 포함).
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.adapters.external_sources import SourceFormatError, build_source, map_items
from app.db.models import Asset, Device, ExternalSource, Organization
from app.domain.ids import new_id
from app.services.observations import OBS
from app.timefmt import iso

if TYPE_CHECKING:
    from app.container import Container

OPERATOR_ORG_TYPE = "OPERATOR"


def operator_org(db: Session) -> str:
    org = db.execute(select(Organization.organization_id).where(Organization.org_type == OPERATOR_ORG_TYPE)
                     .order_by(Organization.organization_id).limit(1)).scalar()
    if org is None:
        raise RuntimeError("운영기관 조직이 없습니다")
    return org


def ensure_stations(c: Container, db: Session, src: ExternalSource) -> dict[str, str]:
    """관측소 → 원천 장비(EXTERNAL_STATION) (요구사항 4.1). 관측소 코드 → device_id."""
    org = operator_org(db)
    out: dict[str, str] = {}
    stations = []
    for st in src.stations:
        dev_id = st.get("device_id")
        if not dev_id or db.get(Device, dev_id) is None:
            dev_id = new_id("device")
            db.add(Device(device_id=dev_id, organization_id=org, project_id=None, name=f"{src.name} {st['name']}",
                          kind="SOURCE", device_type="EXTERNAL_STATION", collector_device_id=None, status="ACTIVE",
                          collection_mode="C3_APPROVED_FULL", local_copy_policy="RETAINED",
                          expected_interval_s=src.interval_s, source_timezone=None, created_at=c.clock.now()))
            db.flush()
        stations.append({**st, "device_id": dev_id})
        out[str(st["code"])] = dev_id
    src.stations = stations
    return out


def due_sources(db: Session, now: datetime) -> list[ExternalSource]:
    rows = db.execute(select(ExternalSource).where(ExternalSource.enabled)).scalars()
    return [s for s in rows if s.last_run_at is None or (now - s.last_run_at).total_seconds() >= s.interval_s]


def schedule(c: Container, db: Session) -> list[str]:
    """소스별 주기로 수집 작업 등록 (요구사항 2.1). 빈 구간은 기준점부터 이어서 = 보충 (요구사항 2.2)."""
    now = c.clock.now()
    out = []
    for s in due_sources(db, now):
        start = s.collected_until or (now - timedelta(seconds=s.interval_s))
        if not s.supports_backfill:  # 과거 조회가 안 되면 지금 구간만
            start = max(start, now - timedelta(seconds=s.interval_s))
        end = min(now, start + timedelta(seconds=s.max_window_s))
        if end <= start:
            continue
        c.queue.enqueue("collect_source", {"source_id": s.source_id, "since": iso(start), "until": iso(end)},
                        dedupe_key=f"collect:{s.source_id}:{int(start.timestamp())}")
        s.last_run_at = now
        out.append(s.source_id)
    return out


def collect(c: Container, source_id: str, since: datetime, until: datetime) -> dict[str, Any]:
    """한 구간 수집 → 저장 → 결합 갱신. 형식 오류는 격리·실패 집계 후 FORMAT_ERROR."""
    from app.services.observations import ingest_items

    now = c.clock.now()
    with c.sessionmaker.begin() as db:
        src = db.get(ExternalSource, source_id, with_for_update=True)
        if src is None or not src.enabled:
            return {"status": "SKIPPED"}
        stations = ensure_stations(c, db, src)
        codes = list(stations)
        if src.rate_limit_per_min and len(codes) > src.rate_limit_per_min:  # 호출 한도 (요구사항 2.3)
            codes = codes[:src.rate_limit_per_min]
        adapter = build_source(src.adapter_kind, broken=bool((src.options or {}).get("simulate_format_change")))
        result = adapter.fetch(codes, since, until)
        body = json.dumps(result.raw, ensure_ascii=False, sort_keys=True).encode()
        sha = hashlib.sha256(body).hexdigest()
        ref = f"external/{src.source_id}/{since.strftime('%Y%m%dT%H%M%S')}-{sha[:12]}.json"
        try:
            items = map_items(result.raw, src.field_mapping)
        except SourceFormatError as exc:
            c.storage.put_object(c.settings.bucket_quarantine, ref, body, "application/json")  # 원 응답 격리 (3.1)
            src.consecutive_failures += 1
            src.last_error, src.format_error_at = str(exc)[:500], now
            return {"status": "FORMAT_ERROR", "quarantined": ref}
        c.storage.put_object(c.settings.bucket_raw, ref, body, "application/json")
        org = operator_org(db)
        stored = 0
        by_station: dict[str, list[dict[str, Any]]] = {}
        for it in items:
            by_station.setdefault(it["station"], []).append(it)
        for code, rows in by_station.items():
            dev = db.get(Device, stations.get(code, ""))
            if dev is None:
                continue
            res = ingest_items(c, db, organization_id=org, device=dev, session_id=None, clock_offset_ms=None,
                               items=[{"metric": r["metric"], "observed_at": r["observed_at"], "value": r["value"],
                                       "unit": r["unit"], "quality_flag": "GOOD"} for r in rows],
                               source_ref=ref)
            stored += res.accepted
        src.collected_until = max(src.collected_until or until, until)
        src.consecutive_failures, src.last_error, src.format_error_at = 0, None, None
        src.last_success_at = now
    refreshed = refresh_env(c, since, until)  # 보충 수집 시 결합 갱신 (요구사항 5.3)
    return {"status": "OK", "stored": stored, "raw_ref": ref, "env_refreshed": refreshed}


# ------------------------------------------------------------------ 환경 정보 결합 (요구사항 5)


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def join_env(c: Container, db: Session, a: Asset) -> dict[str, Any] | None:
    """반경·시간 창 안 가장 가까운 관측값 (관측소 거리·시각 차이 기록). 촬영시각 미상이면 결합하지 않는다."""
    if a.capture_start_at is None or not a.location or a.location.get("type") != "Point":
        return None
    lon, lat = a.location["coordinates"][:2]
    s = c.settings
    mid = a.capture_start_at + ((a.capture_end_at - a.capture_start_at) / 2 if a.capture_end_at else timedelta(0))
    window = timedelta(minutes=s.env_join_window_min)
    near, everywhere = [], []
    for src in db.execute(select(ExternalSource)).scalars():
        for st in src.stations:
            if not st.get("device_id"):
                continue
            d = _km(lat, lon, float(st["lat"]), float(st["lon"]))
            everywhere.append((d, src, st))
            if d <= s.env_join_radius_km:
                near.append((d, src, st))
    if not everywhere:
        return None  # 관측소가 하나도 없으면 결합 대상이 아니다 (경고도 없음)
    entries: dict[str, dict[str, Any]] = {}
    best: dict[str, tuple[float, float]] = {}  # 비교는 반올림 전 값으로 (거리 우선, 다음은 시각 차이)
    for d, src, st in sorted(near, key=lambda x: x[0]):
        rows = db.execute(select(OBS.c.metric, OBS.c.value, OBS.c.unit, OBS.c.observed_at,
                                 func.abs(func.extract("epoch", OBS.c.observed_at - mid)).label("dt"))
                          .where(OBS.c.source_system_id == st["device_id"], OBS.c.observed_at >= mid - window,
                                 OBS.c.observed_at <= mid + window, OBS.c.value.is_not(None))
                          .order_by("dt")).all()
        for metric, value, unit, observed_at, dt in rows:
            if metric in best and best[metric] <= (d, float(dt)):
                continue
            best[metric] = (d, float(dt))
            entries[metric] = {"metric": metric, "value": value, "unit": unit, "observed_at": iso(observed_at),
                                   "station_device_id": st["device_id"], "station_name": st["name"],
                                   "distance_km": round(d, 3), "time_diff_s": int(dt), "source_id": src.source_id,
                                   "attribution": src.attribution, "redistribution_allowed": src.redistribution_allowed}
    head = {"joined_at": iso(c.clock.now()), "radius_km": s.env_join_radius_km, "window_min": s.env_join_window_min}
    if not entries:  # 결합하지 않고 이유를 남긴다 (T17-b)
        return {**head, "observations": [], "warnings": [_no_join_warning(db, near, everywhere, mid, s)]}
    return {**head, "observations": sorted(entries.values(), key=lambda e: e["metric"])}


def _no_join_warning(db: Session, near: list[Any], everywhere: list[Any], mid: datetime, s: Any) -> dict[str, Any]:
    if not near:
        d, _, st = min(everywhere, key=lambda x: x[0])
        msg = f"반경 {s.env_join_radius_km:g}km 안에 관측소가 없습니다 (가장 가까운 {st['name']} {d:.1f}km)"
        return {"code": "NO_STATION_IN_RADIUS", "message": msg,
                "nearest": {"station_device_id": st["device_id"], "station_name": st["name"],
                            "distance_km": round(d, 3)}}
    best: tuple[float, float, dict[str, Any]] | None = None
    for d, _, st in near:
        dt = db.execute(select(func.min(func.abs(func.extract("epoch", OBS.c.observed_at - mid))))
                        .where(OBS.c.source_system_id == st["device_id"], OBS.c.value.is_not(None))).scalar()
        if dt is not None and (best is None or float(dt) < best[1]):
            best = (d, float(dt), st)
    msg = f"촬영 시각 ±{s.env_join_window_min:g}분 안에 반경 안 관측소의 관측값이 없습니다"
    if best is None:
        return {"code": "NO_OBSERVATION_IN_WINDOW", "message": msg + " (관측값 없음)"}
    d, dt, st = best
    return {"code": "NO_OBSERVATION_IN_WINDOW", "message": msg + f" (가장 가까운 관측은 {dt / 60:.0f}분 차이)",
            "nearest": {"station_device_id": st["device_id"], "station_name": st["name"], "distance_km": round(d, 3),
                        "time_diff_s": int(dt)}}


def refresh_env(c: Container, since: datetime, until: datetime) -> int:
    """관측 구간과 촬영 시각이 겹치는 자산의 env_context를 다시 계산."""
    w = timedelta(minutes=c.settings.env_join_window_min)
    n = 0
    with c.sessionmaker.begin() as db:
        rows = db.execute(select(Asset).where(Asset.capture_start_at.is_not(None), Asset.location.is_not(None),
                                              Asset.deleted_at.is_(None),
                                              and_(Asset.capture_start_at >= since - w,
                                                   Asset.capture_start_at <= until + w))).scalars()
        for a in rows:
            env = join_env(c, db, a)
            if env is not None:
                a.env_context = env
                n += 1 if env["observations"] else 0
    return n


def public_env(env: dict[str, Any] | None) -> dict[str, Any] | None:
    """재배포가 허용되지 않은 관측값은 기관 밖으로 내보내지 않는다 (요구사항 6.1)."""
    if not env:
        return env
    obs = [o for o in env.get("observations", []) if o.get("redistribution_allowed")]
    return {**env, "observations": obs} if obs else None


def env_summary(assets: list[Asset]) -> dict[str, str]:
    """보고서 변수 (요구사항 7.1): env.<metric>.max / .mean, env.attribution."""
    vals: dict[str, list[float]] = {}
    attrs: set[str] = set()
    for a in assets:
        for o in (a.env_context or {}).get("observations", []):
            if isinstance(o.get("value"), int | float):
                vals.setdefault(o["metric"], []).append(float(o["value"]))
                attrs.add(o.get("attribution") or "")
    out = {}
    for m, xs in vals.items():
        out[f"env.{m}.max"] = f"{max(xs):g}"
        out[f"env.{m}.mean"] = f"{sum(xs) / len(xs):.2f}"
    out["env.attribution"] = " / ".join(sorted(x for x in attrs if x))
    return out
