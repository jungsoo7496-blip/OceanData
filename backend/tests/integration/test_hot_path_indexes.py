"""ADR-0042: 뜨거운 조회·폴링 경로의 인덱스 (마이그레이션 0024).

앱이 실제로 보내는 SQL과 매개변수를 잡아 같은 값으로 EXPLAIN 한다. 시험 표는 작아서 순차 탐색이 늘 싸므로
`SET LOCAL enable_seqscan = off`로 끄고, 질의 모양에 맞는 인덱스(정렬 없이 limit에서 멈추는 것)를 쓰는지 본다.
누군가 질의의 정렬·조건이나 인덱스를 바꿔 서로 어긋나면 실패한다. 결과·순서가 그대로인지도 함께 본다.
실데이터 규모의 전후 시간은 ADR-0042에 있다. 시험 자료는 모두 합성이다.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Engine

from app.seed import DEV_A_CAMERA, DEV_A_SENSOR, ORG_A, PRJ_A, SES_A

pytestmark = pytest.mark.integration
ADMIN_A = 101
NOW = "CAST(:now AS timestamptz)"  # pg8000은 시각 매개변수에 형을 붙이지 않는다 (시각 ± 간격 계산용)

NEW_INDEXES = {
    "ix_jobs_claim_queued": "ON public.jobs USING btree (priority DESC, run_after, created_at) "
                            "WHERE ((status)::text = 'QUEUED'::text)",
    "ix_uploads_uploading_expires": "ON public.uploads USING btree (expires_at) "
                                    "WHERE ((status)::text = 'UPLOADING'::text)",
    "ix_uploads_created_at": "ON public.uploads USING btree (created_at)",
    "ix_uploads_failed_updated": "ON public.uploads USING btree (updated_at) WHERE (error_code IS NOT NULL)",
    "ix_assets_ingested_at": "ON public.assets USING btree (ingested_at)",
    "ix_assets_not_ready": "ON public.assets USING btree (organization_id, ingest_status, asset_id) "
                           "WHERE ((ingest_status)::text <> 'READY_PRIVATE'::text)",
    "ix_observations_org_time": "ON ONLY public.observations USING btree (organization_id, observed_at)",
}


@contextmanager
def _captured(engine: Engine) -> Iterator[list[tuple[str, Any]]]:
    """이 블록에서 앱이 DB 드라이버로 보낸 SQL·매개변수."""
    seen: list[tuple[str, Any]] = []

    def listener(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        seen.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", listener)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", listener)


def _matching(seen: list[tuple[str, Any]], *needles: str) -> list[tuple[str, Any]]:
    hits = [(s, p) for s, p in seen if all(n in s for n in needles)]
    assert hits, (needles, [s[:120] for s, _ in seen])
    return hits


def _one(seen: list[tuple[str, Any]], *needles: str) -> tuple[str, Any]:
    hits = _matching(seen, *needles)
    assert len(hits) == 1, (needles, [s[:120] for s, _ in hits])
    return hits[0]


def _plan_nodes(engine: Engine, statement: str, params: Any) -> list[dict[str, Any]]:
    """같은 SQL·매개변수의 실행 계획 노드 (순차 탐색을 끄고, 실행하지 않는다)."""
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute("SET LOCAL enable_seqscan = off")
        cur.execute("EXPLAIN (FORMAT JSON) " + statement, params)
        doc = cur.fetchone()[0]
    finally:
        raw.rollback()
        raw.close()
    out, stack = [], [(json.loads(doc) if isinstance(doc, str) else doc)[0]["Plan"]]
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(node.get("Plans", []))
    return out


def _index_names(nodes: list[dict[str, Any]]) -> set[str]:
    return {n["Index Name"] for n in nodes if "Index Name" in n}


def _sorts(nodes: list[dict[str, Any]]) -> list[str]:
    return [n["Node Type"] for n in nodes if n["Node Type"] in ("Sort", "Incremental Sort")]


def _z(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def test_migration_0024_index_definitions(engine) -> None:  # type: ignore[no-untyped-def]
    with engine.begin() as conn:
        defs = dict(conn.execute(text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'")).all())
        for name, tail in NEW_INDEXES.items():
            assert defs.get(name, "").endswith(tail), (name, defs.get(name))
        # 전체 expires_at 인덱스는 진행 중 업로드만 담는 인덱스로 바뀌었고, 기존 기관·상태 인덱스는 그대로다
        assert "ix_uploads_expires_at" not in defs and "ix_assets_org_status" in defs
        # 관측값: 기존 월 파티션·기본 파티션과 새로 만드는 월 파티션 모두 (organization_id, observed_at) 인덱스를 갖는다
        conn.execute(text("SELECT ensure_observation_partition('2029-01-01')"))
        parts = conn.execute(text("""
            SELECT c.relname,
                   EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = c.oid
                             AND pg_get_indexdef(i.indexrelid) LIKE '%(organization_id, observed_at)')
            FROM pg_inherits h JOIN pg_class c ON c.oid = h.inhrelid
            WHERE h.inhparent = 'observations'::regclass""")).all()
    assert len(parts) >= 37 and all(ok for _, ok in parts), [p for p, ok in parts if not ok]
    assert {"observations_default", "observations_202901"} <= {p for p, _ in parts}


def test_queue_claim_walks_queued_index_in_claim_order(ctx) -> None:  # type: ignore[no-untyped-def]
    now = ctx.c.clock.now()
    with ctx.c.engine.begin() as conn:
        # 끝난 작업이 대부분이다(정리하지 않는다). 대기 300건: 우선순위 3종, 그중 30건은 재시도 대기(run_after가 미래)
        conn.execute(text(f"""
            INSERT INTO jobs (job_id, job_type, payload, status, priority, attempts, max_attempts, run_after,
                              created_at, updated_at, resource_class)
            SELECT 'job_' || lpad(g::text, 26, '0'), 'media_probe', '{{}}', 'DONE', 85, 1, 5,
                   {NOW} - interval '1 minute' * g, {NOW} - interval '1 minute' * g, {NOW}, 'CPU'
            FROM generate_series(1, 3000) g"""), {"now": now})
        conn.execute(text(f"""
            INSERT INTO jobs (job_id, job_type, payload, status, priority, attempts, max_attempts, run_after,
                              created_at, updated_at, resource_class)
            SELECT 'job_' || lpad((10000 + g)::text, 26, '0'),
                   (ARRAY['integrity_verify', 'media_probe', 'env_join'])[1 + mod(g, 3)], '{{}}', 'QUEUED',
                   (ARRAY[100, 85, 0])[1 + mod(g, 3)], 0, 5,
                   CASE WHEN mod(g, 10) = 0 THEN {NOW} + interval '1 hour'
                        ELSE {NOW} - interval '1 second' * mod(g, 97) END,
                   {NOW} - interval '1 second' * mod(g, 13), {NOW}, 'CPU'
            FROM generate_series(1, 300) g"""), {"now": now})
        conn.execute(text("ANALYZE jobs"))
        due = conn.execute(text(f"SELECT job_id, priority, run_after, created_at FROM jobs "
                                f"WHERE status = 'QUEUED' AND run_after <= {NOW}"), {"now": now}).all()
    expected = [r[0] for r in sorted(due, key=lambda r: (-r[1], r[2], r[3]))]
    types = ["integrity_verify", "media_probe", "env_join"]
    with _captured(ctx.c.engine) as seen:
        first = ctx.c.queue.claim(types, "w-index", ("CPU",))
    nodes = _plan_nodes(ctx.c.engine, *_one(seen, "FOR UPDATE OF j SKIP LOCKED"))
    assert "ix_jobs_claim_queued" in _index_names(nodes) and not _sorts(nodes), nodes  # 꺼내는 순서를 인덱스가 준다
    got = [first.job_id] if first else []
    while (job := ctx.c.queue.claim(types, "w-index", ("CPU",))) is not None:
        got.append(job.job_id)
    assert len(expected) == 270 and got == expected  # 순서·대상 그대로, run_after가 미래인 30건은 꺼내지 않는다


def _uploads(conn: Any, now: datetime, sql_tail: str, n: int) -> None:
    conn.execute(text(f"""
        INSERT INTO uploads (upload_id, organization_id, asset_id, source_system_id, created_by, bucket, object_key,
                             s3_upload_id, part_size, part_count, declared_size, reserved_bytes, asset_was_metadata_only,
                             status, error_code, expires_at, created_at, updated_at)
        SELECT 'upl_' || lpad(g::text, 26, '0'), :org, NULL, :dev, 'dev', 'raw', 'k/' || g, 'mp-' || g, 8388608, 1, 10,
               0, false, {sql_tail}
        FROM generate_series(1, {n}) g"""), {"org": ORG_A, "dev": DEV_A_CAMERA, "now": now})


def test_upload_sweep_reads_only_open_uploads(ctx, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    from app.services.ingest import IngestService

    aborted: list[str] = []

    def abort(_bucket: str, _key: str, upload_id: str) -> None:
        aborted.append(upload_id)

    monkeypatch.setattr(ctx.c.storage, "abort_multipart_upload", abort, raising=False)
    now = ctx.c.clock.now()
    with ctx.c.engine.begin() as conn:
        # 끝난 업로드 3000건(만료 시각은 모두 지났다) + 진행 중 4건(그중 3건 만료)
        _uploads(conn, now, f"""CASE WHEN g <= 3000 THEN 'RECEIVED' ELSE 'UPLOADING' END, NULL,
                                CASE WHEN g <= 3003 THEN {NOW} - interval '1 hour' * (3004 - g)
                                     ELSE {NOW} + interval '1 day' END,
                                {NOW} - interval '8 days', {NOW} - interval '8 days'""", 3004)
        conn.execute(text("ANALYZE uploads"))
    with _captured(ctx.c.engine) as seen, ctx.c.sessionmaker.begin() as db:
        swept = IngestService(ctx.c, db).sweep_expired()
    nodes = _plan_nodes(ctx.c.engine, *_one(seen, "FROM uploads", "FOR UPDATE SKIP LOCKED"))
    assert "ix_uploads_uploading_expires" in _index_names(nodes) and not _sorts(nodes), nodes
    with ctx.c.engine.connect() as conn:
        left = dict(conn.execute(text("SELECT status, count(*) FROM uploads GROUP BY status")).all())
    assert swept == 3 and left == {"RECEIVED": 3000, "EXPIRED": 3, "UPLOADING": 1}
    assert aborted == ["mp-3001", "mp-3002", "mp-3003"]


def _synthetic_assets(engine: Engine, now: datetime) -> list[str]:
    """60일 동안 들어온 자산 2999건, 가장 최근 59건은 처리 대기(REGISTERED). 대기 자산 ID를 커서 순서로 돌려준다."""
    with engine.begin() as conn:
        conn.execute(text(f"""
            INSERT INTO assets (asset_id, schema_version, organization_id, project_id, session_id, source_system_id,
                                data_type, variant, data_role, ingested_at, timestamp_quality, mime_type, byte_size,
                                checksum_sha256, has_binary, collection_mode, privacy_status, sensitivity_class,
                                sharing_scope, retention_policy_id, retention_until, quality_status, ingest_status,
                                share_status, backup_class, provenance, storage_zone, storage_key, created_at,
                                updated_at)
            SELECT 'ast_' || lpad(g::text, 26, '0'), '1.0', :org, :prj, :ses, :dev, 'VIDEO', 'ORIGINAL', 'CORE',
                   {NOW} - interval '60 days' + interval '1728 seconds' * g, '{{"source": "UNKNOWN", "verified": false}}',
                   'video/mp4', 1000, md5(g::text) || md5((g + 1)::text), true, 'C3_APPROVED_FULL',
                   'REVIEW_REQUIRED', 'RESTRICTED', 'PRIVATE', 'raw_default', {NOW} + interval '3650 days', 'PASSED',
                   CASE WHEN g > 2940 THEN 'REGISTERED' WHEN mod(g, 100) = 0 THEN 'QUARANTINED' ELSE 'READY_PRIVATE' END,
                   'NONE', 'NONE', '{{"ingest_channel": "AGENT"}}', 'RAW', 'k/' || g, {NOW}, {NOW}
            FROM generate_series(1, 2999) g"""), {"org": ORG_A, "prj": PRJ_A, "ses": SES_A, "dev": DEV_A_CAMERA, "now": now})
        conn.execute(text("ANALYZE assets"))
        return list(conn.execute(text("SELECT asset_id FROM assets WHERE ingest_status = 'REGISTERED' "
                                      "ORDER BY asset_id")).scalars())


def test_dashboard_status_list_uses_not_ready_index(ctx) -> None:  # type: ignore[no-untyped-def]
    waiting = _synthetic_assets(ctx.c.engine, ctx.c.clock.now())
    with _captured(ctx.c.engine) as seen:
        r = ctx.client.get("/v1/assets?ingest_status=REGISTERED&limit=50", headers=ctx.as_user(ADMIN_A))
    assert r.status_code == 200, r.text
    # 상태 값은 바인드 매개변수로 가도(pg8000은 실행마다 값을 보고 계획) 부분 인덱스 조건(<> READY_PRIVATE)에 맞는다
    nodes = _plan_nodes(ctx.c.engine, *_one(seen, "FROM assets", "assets.ingest_status =", "LIMIT"))
    assert "ix_assets_not_ready" in _index_names(nodes) and not _sorts(nodes), nodes
    assert [a["asset_id"] for a in r.json()["items"]] == waiting[:50]  # 커서 순서 그대로
    nxt = ctx.client.get(f"/v1/assets?ingest_status=REGISTERED&limit=50&cursor={r.json()['next_cursor']}",
                         headers=ctx.as_user(ADMIN_A)).json()
    assert [a["asset_id"] for a in nxt["items"]] == waiting[50:] and nxt["next_cursor"] is None


def test_watch_rules_and_failure_reasons_read_recent_rows_only(ctx) -> None:  # type: ignore[no-untyped-def]
    from app.services import monitoring

    now = ctx.c.clock.now()
    _synthetic_assets(ctx.c.engine, now)
    with ctx.c.engine.begin() as conn:
        # 60일 동안 업로드 3000건, 100건마다 1건은 실패 코드 (2일에 1건)
        ts = f"({NOW} - interval '60 days' + interval '1728 seconds' * g)"
        _uploads(conn, now, f"""CASE WHEN mod(g, 100) = 0 THEN 'EXPIRED' ELSE 'RECEIVED' END,
                                CASE WHEN mod(g, 100) = 0 THEN 'UPLOAD_EXPIRED' END,
                                {ts} + interval '7 days', {ts}, {ts} + interval '1 minute'""", 3000)
        conn.execute(text("ANALYZE uploads"))
    with _captured(ctx.c.engine) as seen, ctx.c.sessionmaker() as db:
        ectx = monitoring.EvalCtx(db, now, ctx.c.settings)
        list(monitoring._storage_forecast(ectx, monitoring.rule_params(db, "STORAGE_FORECAST")))
        list(monitoring._volume_drop(ectx, monitoring.rule_params(db, "VOLUME_DROP")))
        list(monitoring._upload_failure_rate(ectx, monitoring.rule_params(db, "UPLOAD_FAILURE_RATE")))
        reasons = monitoring.top_failure_reasons(db, now, ORG_A)
    # 용량 예측(1건)·수집량 급감(최근·기준 2건): 최근 며칠치 자산만 읽는다
    recent = _matching(seen, "sum(assets.byte_size)") + _matching(seen, "FROM assets", "assets.variant =")
    assert len(recent) == 3
    for stmt, params in recent:
        assert "ix_assets_ingested_at" in _index_names(_plan_nodes(ctx.c.engine, stmt, params)), stmt
    # 업로드 실패율: 최근 생성분 / 실패 원인: 오류 코드가 있는 최근 업로드
    rate = _plan_nodes(ctx.c.engine, *_one(seen, "FROM uploads", "uploads.created_at >="))
    assert "ix_uploads_created_at" in _index_names(rate), rate
    failed = _plan_nodes(ctx.c.engine, *_one(seen, "FROM uploads", "uploads.updated_at >="))
    assert "ix_uploads_failed_updated" in _index_names(failed), failed
    # 결과는 그대로: 최근 24시간에 바뀐 실패 업로드는 마지막 1건뿐
    assert {"source": "UPLOAD", "code": "UPLOAD_EXPIRED", "count": 1} in reasons


def test_observation_timeline_uses_org_time_index(ctx) -> None:  # type: ignore[no-untyped-def]
    start = datetime(2026, 11, 3, tzinfo=UTC)  # 두 조회가 하나의 월 파티션을 사용한다.
    with ctx.c.engine.begin() as conn:
        # 이틀 동안 1분 간격 4개 항목, 짝수 분만 회차가 붙어 있다
        conn.execute(text(f"""
            INSERT INTO observations (organization_id, source_system_id, metric, observed_at, value, unit,
                                      quality_flag, session_id, received_at)
            SELECT :org, :dev, m, {NOW} + interval '1 minute' * g, 10 + mod(g, 7), u, 'GOOD',
                   CASE WHEN mod(g, 2) = 0 THEN :ses END, {NOW} + interval '1 minute' * g
            FROM generate_series(0, 2879) g,
                 (VALUES ('sea_water_temperature', 'degC'), ('sea_water_salinity', 'PSU'),
                         ('dissolved_oxygen', 'mg/L'), ('turbidity', 'NTU')) v(m, u)"""),
                     {"org": ORG_A, "dev": DEV_A_SENSOR, "ses": SES_A, "now": start})
        conn.execute(text("ANALYZE observations"))
    first = start + timedelta(hours=6)
    window = f"from={_z(first)}&to={_z(start + timedelta(hours=18))}&limit=100"
    with _captured(ctx.c.engine) as seen:  # 시간축(기관 전체)과 회차 필터 — 장비·항목을 거르지 않는다
        r = ctx.client.get(f"/v1/observations?{window}", headers=ctx.as_user(ADMIN_A))
        s = ctx.client.get(f"/v1/observations?{window}&session_id={SES_A}", headers=ctx.as_user(ADMIN_A))
    assert r.status_code == 200 and s.status_code == 200, (r.text, s.text)
    statements = _matching(seen, "FROM observations")
    assert len(statements) == 2
    for stmt, params in statements:  # 파티션의 (organization_id, observed_at) 인덱스로 시각 범위만 읽는다
        names = _index_names(_plan_nodes(ctx.c.engine, stmt, params))
        assert any(n.endswith("_organization_id_observed_at_idx") for n in names), (stmt, names)
    items = r.json()["items"]
    assert len(items) == 100 and items[0]["observed_at"] == _z(first)
    assert [(i["observed_at"], i["metric"]) for i in items] == sorted((i["observed_at"], i["metric"]) for i in items)
    # 회차 필터: 회차가 붙은 짝수 분 관측값만, 시각 순서대로 (25개 시각 × 4개 항목)
    want = [_z(first + timedelta(minutes=2 * k)) for k in range(25)]
    assert sorted({i["observed_at"] for i in s.json()["items"]}) == want and len(s.json()["items"]) == 100
