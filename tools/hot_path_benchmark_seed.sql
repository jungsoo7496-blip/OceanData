\set ON_ERROR_STOP on
-- Run with psql -X -d marine_perf_<suffix> -f tools/hot_path_benchmark_seed.sql
-- as the owner of an otherwise empty PostgreSQL 16 database migrated to 0023. This fixture
-- creates only synthetic rows, with a fixed measurement time of 2026-11-05 09:00 UTC.
BEGIN;
SET LOCAL search_path TO pg_catalog, public;
SET LOCAL TIME ZONE 'UTC';

DO $$
BEGIN
    IF current_database() !~ '^marine_perf_[a-z0-9][a-z0-9_]*$' THEN
        RAISE EXCEPTION 'Benchmark fixture requires a marine_perf_<suffix> database, got %', current_database();
    END IF;
    IF current_setting('server_version_num')::integer / 10000 <> 16 THEN
        RAISE EXCEPTION 'Benchmark fixture requires PostgreSQL 16';
    END IF;
    IF (SELECT array_agg(version_num::text) FROM public.alembic_version)
       IS DISTINCT FROM ARRAY['0023'] THEN
        RAISE EXCEPTION 'Benchmark fixture requires exactly migration 0023';
    END IF;
END $$;

LOCK TABLE organizations, projects, devices, sessions, session_devices, org_quotas, assets,
    jobs, uploads, observations, audit_log, events, webhook_subscriptions, webhook_deliveries,
    tier_moves, incidents, datasets, dataset_versions, dataset_version_items IN SHARE ROW EXCLUSIVE MODE;

DO $$
DECLARE
    table_name text;
    has_rows boolean;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'organizations', 'projects', 'devices', 'sessions', 'session_devices', 'org_quotas', 'assets',
        'jobs', 'uploads', 'observations', 'audit_log', 'events', 'webhook_subscriptions', 'webhook_deliveries',
        'tier_moves', 'incidents', 'datasets', 'dataset_versions', 'dataset_version_items'
    ] LOOP
        EXECUTE format('SELECT EXISTS (SELECT 1 FROM public.%I)', table_name) INTO has_rows;
        IF has_rows THEN
            RAISE EXCEPTION 'Benchmark fixture requires an empty %.% table', 'public', table_name;
        END IF;
    END LOOP;
END $$;

CREATE TEMP TABLE perf_calendar ON COMMIT DROP AS
SELECT timestamptz '2026-01-01 00:00:00+00' AS begins,
       timestamptz '2026-11-05 09:00:00+00' AS as_of;

CREATE FUNCTION pg_temp.perf_id(prefix text, n bigint) RETURNS text
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
RETURN prefix || '_' || lpad(n::text, 26, '0');

CREATE FUNCTION pg_temp.perf_org(n bigint) RETURNS integer
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
RETURN CASE WHEN (n - 1) % 20 < 10 THEN 1 WHEN (n - 1) % 20 < 17 THEN 2 ELSE 3 END;

INSERT INTO organizations (organization_id, name, org_type, created_at)
SELECT pg_temp.perf_id('org', n), 'Synthetic provider ' || n, 'PROVIDER', begins
FROM generate_series(1, 3) n CROSS JOIN perf_calendar;

INSERT INTO projects (project_id, organization_id, name, created_at)
SELECT pg_temp.perf_id('prj', n), pg_temp.perf_id('org', n), 'Synthetic project ' || n, begins
FROM generate_series(1, 3) n CROSS JOIN perf_calendar;

CREATE TEMP TABLE perf_devices ON COMMIT DROP AS
SELECT n, CASE WHEN n <= 20 OR n BETWEEN 41 AND 55 OR n BETWEEN 71 AND 77 THEN 1
               WHEN n <= 34 OR n BETWEEN 56 AND 65 OR n BETWEEN 78 AND 82 THEN 2
               ELSE 3 END AS org_n,
       CASE WHEN n <= 40 THEN 'CAMERA' WHEN n <= 70 THEN 'SENSOR' ELSE 'AGENT_PC' END AS device_type
FROM generate_series(1, 84) n;

INSERT INTO devices (device_id, organization_id, project_id, name, kind, device_type,
                     status, collection_mode, expected_interval_s, created_at)
SELECT pg_temp.perf_id('dev', n), pg_temp.perf_id('org', org_n), pg_temp.perf_id('prj', org_n),
       'Synthetic device ' || n, CASE WHEN n <= 70 THEN 'SOURCE' ELSE 'COLLECTOR' END,
       device_type, 'ACTIVE', 'C3_APPROVED_FULL', CASE WHEN device_type = 'SENSOR' THEN 1800 END, begins
FROM perf_devices CROSS JOIN perf_calendar;

-- 397 historic time slots per organization, then 11 active sessions. Only the
-- first active session per organization has recent data. A's is ses_...1192.
INSERT INTO sessions (session_id, organization_id, project_id, label, title, starts_at, ends_at, created_at)
SELECT pg_temp.perf_id('ses', n), pg_temp.perf_id('org', org_n), pg_temp.perf_id('prj', org_n),
       'PERF-' || n, 'Synthetic trial ' || n, starts_at,
       CASE WHEN n < 1192 THEN starts_at + interval '12 hours' END, begins
FROM generate_series(1, 1202) n CROSS JOIN perf_calendar
CROSS JOIN LATERAL (
    SELECT 1 + (n - 1) % 3 AS org_n,
           CASE WHEN n < 1192 THEN begins + ((n - 1) / 3) * interval '66600 seconds'
                ELSE as_of - interval '12 hours' END AS starts_at
) s;

INSERT INTO session_devices (session_id, device_id)
SELECT pg_temp.perf_id('ses', n),
       pg_temp.perf_id('dev', (ARRAY[1, 21, 35])[1 + (n - 1) % 3])
FROM generate_series(1, 1202) n;

-- Asset IDs track ingestion time. The three institutions have 50%, 35%, 15%
-- of assets, with 42% video originals, 10% sensors, 2% images and 46% derivatives.
CREATE TEMP TABLE perf_asset_shape ON COMMIT DROP AS
SELECT n, org_n, elapsed_s, kind,
       CASE WHEN kind >= 21 AND kind < 26 THEN (ARRAY[41, 56, 66])[org_n] + block_n % (ARRAY[15, 10, 5])[org_n]
            ELSE (ARRAY[1, 21, 35])[org_n] + block_n % (ARRAY[20, 14, 6])[org_n] END AS device_n
FROM generate_series(1, 500000) n
CROSS JOIN LATERAL (
    SELECT pg_temp.perf_org(n) AS org_n, (n - 1) / 20 AS block_n,
           ((n - 1) / 20 + (n - 1) % 20) % 50 AS kind,
           (n::bigint * 26643540 / 500000)::integer AS elapsed_s
) shape;

-- Materialize the small recent selections once for consistent assets/uploads.
CREATE TEMP TABLE perf_open_upload_assets ON COMMIT DROP AS
SELECT n, row_number() OVER (ORDER BY n DESC) AS upload_rank
FROM (SELECT n FROM perf_asset_shape WHERE kind < 27 ORDER BY n DESC LIMIT 180) a;

CREATE TEMP TABLE perf_recent_quarantine ON COMMIT DROP AS
SELECT a.n FROM perf_asset_shape a
LEFT JOIN perf_open_upload_assets u USING (n)
WHERE a.n > 497500 AND u.n IS NULL ORDER BY a.n DESC LIMIT 25;

CREATE TEMP TABLE perf_processing_assets ON COMMIT DROP AS
SELECT n FROM (
    SELECT a.n, a.org_n, row_number() OVER (PARTITION BY a.org_n ORDER BY a.n DESC) AS processing_rank
    FROM perf_asset_shape a
    LEFT JOIN perf_open_upload_assets u USING (n)
    LEFT JOIN perf_recent_quarantine q USING (n)
    WHERE a.n > 497500 AND u.n IS NULL AND q.n IS NULL
) ranked WHERE processing_rank <= (ARRAY[819, 573, 246])[org_n];

INSERT INTO assets (asset_id, schema_version, organization_id, project_id, session_id, source_system_id,
                    data_type, variant, data_role, capture_start_at, capture_end_at, ingested_at,
                    timestamp_quality, mime_type, byte_size, checksum_sha256, has_binary, collection_mode,
                    privacy_status, sensitivity_class, sharing_scope, retention_policy_id, retention_until,
                    quality_status, ingest_status, quarantine_reason, share_status, backup_class, provenance,
                    storage_zone, storage_key, deleted_at, created_at, updated_at)
SELECT pg_temp.perf_id('ast', a.n), '1.0', org_id, prj_id, session_id, dev_id,
       CASE WHEN kind BETWEEN 21 AND 25 THEN 'SENSOR' WHEN kind = 26 OR kind >= 44 THEN 'IMAGE' ELSE 'VIDEO' END,
       variant, 'CORE', ingested - interval '10 minutes', ingested, ingested,
       '{"source":"DEVICE","verified":false}',
       CASE WHEN kind BETWEEN 21 AND 25 THEN 'text/csv' WHEN kind = 26 OR kind >= 44 THEN 'image/jpeg' ELSE 'video/mp4' END,
       CASE WHEN kind < 21 THEN 104857600 ELSE 524288 END + (a.n % 19) * 10000,
       encode(sha256(convert_to('perf-asset-' || a.n, 'UTF8')), 'hex'), true, 'C3_APPROVED_FULL',
       'REVIEW_REQUIRED', 'RESTRICTED', 'PRIVATE',
       CASE WHEN quarantined THEN 'quarantine_corrupt'
            ELSE (ARRAY['raw_default', 'proxy_default', 'thumbnail_default'])[variant_n] END,
       ingested + CASE WHEN quarantined THEN interval '30 days' ELSE interval '3650 days' END,
       CASE WHEN quarantined THEN 'FAILED' WHEN u.n IS NOT NULL THEN 'PENDING' ELSE 'PASSED' END,
       CASE WHEN quarantined THEN 'QUARANTINED' WHEN u.n IS NOT NULL THEN 'UPLOADING'
            WHEN p.n IS NOT NULL THEN 'REGISTERED' ELSE 'READY_PRIVATE' END,
       CASE WHEN quarantined THEN 'CORRUPT_FILE' END, 'NONE', 'NONE', '{"ingest_channel":"AGENT"}',
       CASE WHEN quarantined THEN 'QUARANTINE' WHEN variant_n = 1 THEN 'RAW' ELSE 'PROCESSED' END,
       concat_ws('/', org_id, prj_id, coalesce(session_id, '_unassigned'), dev_id, pg_temp.perf_id('ast', a.n), variant),
       CASE WHEN a.n <= 493700 AND a.n % 50 = 0 THEN ingested + interval '1 day' END,
       ingested, ingested
FROM perf_asset_shape a CROSS JOIN perf_calendar
LEFT JOIN perf_open_upload_assets u USING (n)
LEFT JOIN perf_recent_quarantine q USING (n)
LEFT JOIN perf_processing_assets p USING (n)
CROSS JOIN LATERAL (
    SELECT pg_temp.perf_id('org', org_n) AS org_id, pg_temp.perf_id('prj', org_n) AS prj_id,
           pg_temp.perf_id('dev', device_n) AS dev_id, begins + elapsed_s * interval '1 second' AS ingested,
           CASE WHEN begins + elapsed_s * interval '1 second' >= as_of - interval '12 hours'
                THEN pg_temp.perf_id('ses', 1191 + org_n)
                WHEN elapsed_s / 66600 < 397 AND elapsed_s % 66600 < 43200
                THEN pg_temp.perf_id('ses', elapsed_s / 66600 * 3 + org_n) END AS session_id,
           CASE WHEN kind < 27 THEN 1 WHEN kind < 44 THEN 2 ELSE 3 END AS variant_n,
           q.n IS NOT NULL OR (a.n <= 497500 AND (a.n - 1) % 100 = ((a.n - 1) / 100) % 100) AS quarantined
) dimensions
CROSS JOIN LATERAL (SELECT (ARRAY['ORIGINAL', 'PROXY', 'THUMBNAIL'])[variant_n] AS variant) v
ORDER BY a.n;

-- One upload per original, plus 1,719 cancelled/failed historical attempts.
-- 180 remain open (30 expired), and 6,793 have an error code (~2.5%).
WITH originals AS (
    SELECT a.*, row_number() OVER (ORDER BY a.n) AS upload_n, u.upload_rank
    FROM perf_asset_shape a LEFT JOIN perf_open_upload_assets u USING (n) WHERE kind < 27
), attempts AS (
    SELECT upload_n, n, upload_rank, false AS extra, NULL::bigint AS extra_n FROM originals
    UNION ALL
    SELECT 270000 + retry, n, NULL, true, retry FROM generate_series(1, 1719) retry
    JOIN originals ON upload_n = 268000 + retry
)
INSERT INTO uploads (upload_id, organization_id, asset_id, source_system_id, created_by, bucket, object_key,
                     s3_upload_id, part_size, part_count, declared_size, reserved_bytes, status, error_code,
                     expires_at, created_at, updated_at)
SELECT pg_temp.perf_id('upl', upload_n), asset.organization_id, CASE WHEN NOT extra THEN asset.asset_id END,
       asset.source_system_id, 'perf-agent', 'perf-raw', asset.storage_key, 'perf-multipart-' || upload_n,
       8388608, ((asset.byte_size + 8388607) / 8388608)::integer, asset.byte_size,
       CASE WHEN upload_rank IS NULL THEN 0 ELSE asset.byte_size END,
       CASE WHEN extra THEN 'ABORTED' WHEN upload_rank IS NOT NULL THEN 'UPLOADING' ELSE 'RECEIVED' END,
       CASE WHEN (extra AND extra_n <= 48) OR (NOT extra AND upload_rank IS NULL AND upload_n % 40 = 0)
            THEN 'CHECKSUM_MISMATCH' END,
       CASE WHEN upload_rank <= 30 THEN as_of - upload_rank * interval '1 minute'
            WHEN upload_rank IS NOT NULL THEN as_of + upload_rank * interval '1 minute'
            ELSE least(opened_at + interval '1 day', as_of - interval '1 minute') END,
       opened_at, opened_at
FROM attempts CROSS JOIN perf_calendar JOIN assets asset ON asset.asset_id = pg_temp.perf_id('ast', attempts.n)
CROSS JOIN LATERAL (
    SELECT CASE WHEN extra AND extra_n <= 48 THEN as_of - extra_n * interval '1 minute'
                WHEN upload_rank <= 30 THEN as_of - interval '1 day' - upload_rank * interval '1 minute'
                WHEN upload_rank IS NOT NULL THEN as_of - upload_rank * interval '1 minute'
                ELSE asset.ingested_at - interval '1 hour' END AS opened_at
) timing
ORDER BY upload_n;

INSERT INTO org_quotas (organization_id, quota_bytes, used_bytes, quarantine_bytes, reserved_bytes, updated_at)
SELECT organization_id, sum(byte_size) * 2,
       coalesce(sum(byte_size) FILTER (WHERE storage_zone <> 'QUARANTINE' AND ingest_status <> 'UPLOADING'), 0),
       coalesce(sum(byte_size) FILTER (WHERE storage_zone = 'QUARANTINE'), 0),
       coalesce(sum(byte_size) FILTER (WHERE ingest_status = 'UPLOADING'), 0), max(as_of)
FROM assets CROSS JOIN perf_calendar WHERE deleted_at IS NULL GROUP BY organization_id;

-- Done: 1,087,987. Running: 9. Quiet queue: 160, including 100 future retries.
-- Extra burst: 40,000 due jobs, 10,000 of each priority. For a quiet run,
-- temporarily DELETE FROM jobs WHERE job_id >= 'job_' || lpad('1088157', 26, '0'), then roll back.
WITH work AS (
    SELECT n, 1 + (n - 1) % 4 AS job_n, 1 + (n * 283) % 500000 AS asset_n
    FROM generate_series(1::bigint, 1128156) n
)
INSERT INTO jobs (job_id, job_type, payload, status, priority, attempts, run_after, locked_by, locked_at,
                  organization_id, created_at, updated_at, asset_id, lease_expires_at)
SELECT pg_temp.perf_id('job', work.n), (ARRAY['integrity_verify', 'media_probe', 'env_join', 'make_proxy'])[job_n],
       jsonb_build_object('asset_id', pg_temp.perf_id('ast', asset_n)),
       CASE WHEN work.n <= 1087987 THEN 'DONE' WHEN work.n <= 1087996 THEN 'RUNNING' ELSE 'QUEUED' END,
       (ARRAY[100, 85, 0, 80])[job_n], CASE WHEN work.n <= 1087996 OR work.n BETWEEN 1088057 AND 1088156 THEN 1 ELSE 0 END,
       CASE WHEN work.n BETWEEN 1088057 AND 1088156 THEN as_of + (work.n % 3600) * interval '1 second'
            ELSE created + interval '1 second' END,
       CASE WHEN work.n BETWEEN 1087988 AND 1087996 THEN 'perf-worker' END,
       CASE WHEN work.n BETWEEN 1087988 AND 1087996 THEN created END,
       pg_temp.perf_id('org', pg_temp.perf_org(asset_n)), created, created + interval '1 second',
       pg_temp.perf_id('ast', asset_n),
       CASE WHEN work.n BETWEEN 1087988 AND 1087996 THEN as_of + interval '5 minutes' END
FROM work CROSS JOIN perf_calendar
CROSS JOIN LATERAL (
    SELECT CASE WHEN work.n <= 1087987 THEN begins + (work.n * 26643540 / 1087987) * interval '1 second'
                ELSE as_of - (10 + (work.n * 239) % 172800) * interval '1 second' END AS created
) timing
ORDER BY work.n;

-- 30 sensors x 4 metrics x 14,802 half-hour slots in existing monthly partitions.
INSERT INTO observations (organization_id, source_system_id, metric, observed_at, value, unit,
                          quality_flag, session_id, received_at)
SELECT pg_temp.perf_id('org', org_n), pg_temp.perf_id('dev', n), metric, observed_at,
       value_base + ((slot + n) % 70) / 10.0, unit, 'GOOD',
       CASE WHEN observed_at >= as_of - interval '12 hours' THEN
                CASE WHEN (slot + n) % 3 <> 0 THEN pg_temp.perf_id('ses', 1191 + org_n) END
            WHEN slot * 1800 / 66600 < 397 AND slot * 1800 % 66600 < 43200
            THEN pg_temp.perf_id('ses', slot * 1800 / 66600 * 3 + org_n) END,
       observed_at + interval '20 seconds'
FROM generate_series(0, 14801) slot CROSS JOIN perf_calendar CROSS JOIN perf_devices
CROSS JOIN (VALUES ('sea_water_temperature', 'degC', 10), ('sea_water_salinity', 'PSU', 28),
                   ('dissolved_oxygen', 'mg/L', 3), ('turbidity', 'NTU', 1)) metrics(metric, unit, value_base)
CROSS JOIN LATERAL (SELECT begins + slot * interval '30 minutes' AS observed_at) timing
WHERE device_type = 'SENSOR';

-- Deterministic hash-shaped placeholders: these rows measure audit list reads,
-- and are not a fixture for validating the application audit chain.
INSERT INTO audit_log (audit_id, occurred_at, actor_kind, actor_id, actor_organization_id,
                       organization_id, action, target_type, target_id, result, details, prev_hash, row_hash)
SELECT pg_temp.perf_id('aud', n), begins + (n * 26643540 / 1500000) * interval '1 second',
       'SERVICE', 'perf-fixture', org_id, org_id, 'UPLOAD_REGISTERED', 'Asset', pg_temp.perf_id('ast', asset_n),
       'SUCCESS', '{"synthetic":true}',
       CASE WHEN n = 1 THEN repeat('0', 64) ELSE encode(sha256(convert_to('perf-audit-' || (n - 1), 'UTF8')), 'hex') END,
       encode(sha256(convert_to('perf-audit-' || n, 'UTF8')), 'hex')
FROM generate_series(1::bigint, 1500000) n CROSS JOIN perf_calendar
CROSS JOIN LATERAL (SELECT 1 + (n - 1) % 500000 AS asset_n) shape
CROSS JOIN LATERAL (SELECT pg_temp.perf_id('org', pg_temp.perf_org(asset_n)) AS org_id) scope
ORDER BY n;

INSERT INTO events (event_id, organization_id, session_id, project_id, event_code, started_at, ended_at,
                    timestamp_quality, source, schema_version, created_by, created_at)
SELECT pg_temp.perf_id('evt', n), s.organization_id, s.session_id, s.project_id, 'VESSEL',
       observed_at, observed_at + interval '20 seconds', '{"source":"MANUAL","verified":false}',
       'MANUAL', '1.0', 'perf-fixture', observed_at
FROM generate_series(1, 200000) n
JOIN sessions s ON s.session_id = pg_temp.perf_id('ses', (n - 1) * 397 / 200000 * 3 + pg_temp.perf_org(n))
CROSS JOIN LATERAL (SELECT s.starts_at + (n * 137 % 43200) * interval '1 second' AS observed_at) timing
ORDER BY n;

INSERT INTO webhook_subscriptions (webhook_id, organization_id, url, event_types, secret_salt, created_by, created_at)
SELECT pg_temp.perf_id('whk', n), pg_temp.perf_id('org', n), 'https://example.invalid/perf/' || n,
       ARRAY['upload.registered'], repeat('0', 64), 'perf-fixture', begins
FROM generate_series(1, 3) n CROSS JOIN perf_calendar;

-- Pending deliveries: 25 due and 275 waiting for retry.
INSERT INTO webhook_deliveries (delivery_id, event_id, webhook_id, organization_id, event_type, payload,
                                status, attempts, next_attempt_at, last_status_code, created_at, delivered_at)
SELECT lpad(n::text, 26, '0'), lpad(n::text, 26, '0'), pg_temp.perf_id('whk', org_n), pg_temp.perf_id('org', org_n),
       'upload.registered', '{"synthetic":true}',
       CASE WHEN n <= 149700 THEN 'DELIVERED' ELSE 'PENDING' END,
       CASE WHEN n BETWEEN 149701 AND 149725 THEN 0 ELSE 1 END,
       CASE WHEN n <= 149700 THEN created WHEN n <= 149725 THEN as_of - interval '1 minute'
            ELSE as_of + (n - 149725) * interval '1 minute' END,
       CASE WHEN n <= 149700 THEN 204 WHEN n > 149725 THEN 503 END,
       created, CASE WHEN n <= 149700 THEN created + interval '1 second' END
FROM generate_series(1::bigint, 150000) n CROSS JOIN perf_calendar
CROSS JOIN LATERAL (SELECT pg_temp.perf_org(n) AS org_n, begins + n * 177 * interval '1 second' AS created) shape;

-- Returned-to-HOT history plus five switched moves (three due for source cleanup).
INSERT INTO tier_moves (asset_id, organization_id, policy_id, from_tier, to_tier, zone, object_key,
                        byte_size, checksum_sha256, status, created_at, updated_at, switched_at, delete_after, finished_at)
SELECT asset_id, organization_id, NULL, 'WARM', 'HOT', storage_zone, storage_key, byte_size, checksum_sha256,
       CASE WHEN rank_n <= 3000 THEN 'DONE' ELSE 'SWITCHED' END,
       switched - interval '1 hour', switched, switched,
       CASE WHEN rank_n <= 3003 THEN switched + interval '2 minutes' ELSE as_of + interval '1 hour' END,
       CASE WHEN rank_n <= 3000 THEN switched + interval '3 minutes' END
FROM (
    SELECT a.*, row_number() OVER (ORDER BY asset_id) AS rank_n
    FROM assets a WHERE storage_zone = 'RAW' AND ingest_status = 'READY_PRIVATE' AND deleted_at IS NULL
    ORDER BY asset_id LIMIT 3005
) candidates CROSS JOIN perf_calendar
CROSS JOIN LATERAL (
    SELECT CASE WHEN rank_n <= 3000 THEN as_of - (3006 - rank_n) * interval '1 hour'
                ELSE as_of - interval '30 minutes' END AS switched
) timing
ORDER BY rank_n;

INSERT INTO incidents (incident_id, alert_key, rule_code, severity, status, organization_id,
                       session_id, summary, guidance, opened_at, resolved_at)
SELECT pg_temp.perf_id('inc', n), 'SESSION_NO_DATA:' || s.session_id, 'SESSION_NO_DATA', 'WARN',
       CASE WHEN n <= 2975 THEN 'RESOLVED' ELSE 'OPEN' END, s.organization_id, s.session_id,
       'Synthetic trial without new data', 'Synthetic guidance', opened,
       CASE WHEN n <= 2975 THEN opened + interval '1 hour' END
FROM generate_series(1, 3000) n CROSS JOIN perf_calendar
JOIN sessions s ON s.session_id = pg_temp.perf_id('ses', 1 + (n - 1) % 1202)
CROSS JOIN LATERAL (SELECT as_of - (3001 - n) * interval '1 hour' AS opened) timing;

INSERT INTO datasets (dataset_id, title, published, published_at, created_by, created_at)
SELECT pg_temp.perf_id('dst', n), 'Synthetic dataset ' || n, true, begins, 'perf-fixture', begins
FROM generate_series(1, 200) n CROSS JOIN perf_calendar;

INSERT INTO dataset_versions (dataset_version_id, dataset_id, version, provider_organization_ids, item_count,
                              total_bytes, data_card, manifest, manifest_key, manifest_sha256, created_by, created_at)
SELECT pg_temp.perf_id('dsv', n), pg_temp.perf_id('dst', (n + 1) / 2), 'v' || (1 + (n - 1) % 2),
       ARRAY[pg_temp.perf_id('org', pg_temp.perf_org(n))], 0, 0, '{}', '{}', 'perf-manifest/' || n,
       encode(sha256(convert_to('perf-manifest-' || n, 'UTF8')), 'hex'), 'perf-fixture', begins + n * interval '1 hour'
FROM generate_series(1, 400) n CROSS JOIN perf_calendar;

INSERT INTO dataset_version_items (dataset_version_id, asset_id, organization_id, path, object_key, byte_size, checksum_sha256)
SELECT pg_temp.perf_id('dsv', 1 + (n - 1) / 100), asset_id, organization_id, 'perf-item/' || n,
       storage_key, byte_size, checksum_sha256
FROM generate_series(1, 39887) n
JOIN assets ON asset_id = pg_temp.perf_id('ast', n * 10 + 1);

UPDATE dataset_versions v SET item_count = totals.item_count, total_bytes = totals.total_bytes,
       provider_organization_ids = totals.providers
FROM (
    SELECT dataset_version_id, count(*) AS item_count, sum(byte_size) AS total_bytes,
           array_agg(DISTINCT organization_id ORDER BY organization_id) AS providers
    FROM dataset_version_items GROUP BY dataset_version_id
) totals WHERE totals.dataset_version_id = v.dataset_version_id;

ANALYZE organizations, projects, devices, sessions, session_devices, org_quotas, assets,
    jobs, uploads, observations, audit_log, events, webhook_subscriptions, webhook_deliveries,
    tier_moves, incidents, datasets, dataset_versions, dataset_version_items;
COMMIT;
