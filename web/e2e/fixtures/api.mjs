// @ts-check
// 접근성 검사(e2e/a11y-mock.mjs)용 합성 /v1 응답 (09번 작업 15, 요구사항 11.2).
// 실제 스택 대신 계약(contracts/openapi.yaml) 모양의 값을 돌려준다. 모양은 계약에서 생성한 타입(src/contract/api.gen.ts)으로
// `npm run typecheck`(e2e/tsconfig.json)가 확인하므로, 계약이 바뀌어 맞지 않게 되면 타입 검사가 실패한다.
// 값은 모두 합성이다. 실제 기관·사람·장비·영상·좌표가 아니다.

/** @typedef {import('../../src/contract/api.gen').paths} Paths */
/** @typedef {import('../../src/contract/api.gen').components['schemas']} S */
/** @typedef {S['role']} Role */
/** @typedef {S['asset-metadata.schema']} Asset */
/**
 * 응답 하나의 application/json 본문
 * @template T
 * @typedef {T extends { content: { 'application/json': infer R } } ? R : never} JsonOf
 */
/**
 * GET 200 응답 본문
 * @template {keyof Paths} P
 * @typedef {Paths[P] extends { get: { responses: { 200: infer R } } } ? JsonOf<R> : never} Get
 */
/**
 * POST 2xx 응답 본문
 * @template {keyof Paths} P
 * @typedef {Paths[P] extends { post: { responses: infer Rs } } ? JsonOf<Rs[Extract<keyof Rs, 200 | 201 | 202>]> : never} Post
 */
/**
 * @typedef {object} Ctx
 * @property {Role} role 로그인한 사용자 역할 (검사는 역할 하나씩)
 * @property {URLSearchParams} query
 * @property {Record<string, string>} params 경로 변수 ({asset_id} 등)
 * @property {unknown} body POST 본문 (JSON)
 * @property {{ video: string, image: string }} media 합성 시험 패턴 data: URL (fixtures/testsrc.webm·jpg)
 */
/** @typedef {{ [P in keyof Paths]?: (ctx: Ctx) => Get<P> }} GetRoutes */
/** @typedef {{ [P in keyof Paths]?: (ctx: Ctx) => { status: 200 | 201 | 202, json: Post<P> } }} PostRoutes */

// ---------------------------------------------------------------- 공통 값

/** 접두사 + ULID 모양(Crockford Base32 26자) 합성 ID @param {string} prefix @param {number} n */
const id = (prefix, n) => `${prefix}_01K9A11Y${String(n).padStart(18, '0')}`
/** 합성 sha256 (64자리 16진수) @param {number} n */
const sha = (n) => n.toString(16).padStart(64, '0')
/** 회차 날짜(2026-11-03, UTC)의 시각 @param {string} hms 'HH:MM:SS' */
const at = (hms) => `2026-11-03T${hms}Z`
const LATER = '2026-11-05T01:00:00Z'

/**
 * 계약이 속성 없이 object로만 정한 자유 형식 값. 생성 타입은 Record<string, never>라 값을 담을 수 없어 이 지점만 타입 검사를 건너뛴다.
 * 화면이 읽는 필드는 백엔드가 채우는 이름을 따른다
 * @param {Record<string, unknown>} value
 * @returns {any}
 */
const free = (value) => value


const ORG_A = id('org', 1) // 제공기관 A
const ORG_B = id('org', 2) // 제공기관 B
const ORG_PROGRAM = id('org', 3) // 관리기관
const ORG_OPERATOR = id('org', 4) // 운영기관
const ORG_CONSUMER = id('org', 5) // 이용기관
const USR_ADMIN = id('usr', 1)
const USR_MEMBER = id('usr', 2)
const USR_OPERATOR = id('usr', 3)
const USR_OPERATOR2 = id('usr', 4)
const USR_REVIEWER = id('usr', 5)
const PRJ = id('prj', 1)
const SES1 = id('ses', 1)
const SES2 = id('ses', 2)
const DEV_PC = id('dev', 1)
const DEV_CAM = id('dev', 2)
const DEV_SENSOR = id('dev', 3)
const DEV_B_PC = id('dev', 4)
export const AST_VIDEO = id('ast', 1)
export const AST_PROXY = id('ast', 2)
const AST_CLIP = id('ast', 3)
const AST_CSV = id('ast', 4)
const AST_IMAGE = id('ast', 5)
const AST_QUARANTINED = id('ast', 6)
const AST_RECEIVED = id('ast', 7)
const AST_B_QUARANTINED = id('ast', 8)
const AST_CANDIDATE = id('ast', 9)
const AST_LEGAL = id('ast', 10)
const AST_QUALITY = id('ast', 11)
const AST_DEID = id('ast', 12)
const EVT_VESSEL = id('evt', 1)
const EVT_FLOAT = id('evt', 2)
const EVT_FAULT = id('evt', 3)
const REC1 = id('rec', 1)
const REC2 = id('rec', 2)
export const RPT_DRAFT = id('rpt', 1)
const RPT_APPROVED = id('rpt', 2)
export const RPT_SUBMITTED = id('rpt', 3)
const DST1 = id('dst', 1)
const DST2 = id('dst', 2)
const DSV1 = id('dsv', 1)
const DSV2 = id('dsv', 2)
const ACR1 = id('acr', 1)
const ACR2 = id('acr', 2)
const ACR3 = id('acr', 3)
const GRT1 = id('grt', 1)
const GRT2 = id('grt', 2)
const RVW1 = id('rvw', 1)
export { SES1 }

// ---------------------------------------------------------------- 사용자·기관

/** @type {Partial<Record<Role, { org: string, org_type: S['org_type'], user: string, mfa: boolean }>>} */
const PRINCIPALS = {
  ORG_ADMIN: { org: ORG_A, org_type: 'PROVIDER', user: USR_ADMIN, mfa: true },
  ORG_MEMBER: { org: ORG_A, org_type: 'PROVIDER', user: USR_MEMBER, mfa: false },
  PLATFORM_OPERATOR: { org: ORG_OPERATOR, org_type: 'OPERATOR', user: USR_OPERATOR, mfa: true },
  PROGRAM_VIEWER: { org: ORG_PROGRAM, org_type: 'PROGRAM_AGENCY', user: id('usr', 6), mfa: false },
  REVIEWER: { org: ORG_OPERATOR, org_type: 'OPERATOR', user: USR_REVIEWER, mfa: true },
  CONSUMER: { org: ORG_CONSUMER, org_type: 'CONSUMER', user: id('usr', 7), mfa: false },
}

const isOrg = (/** @type {Role} */ r) => r === 'ORG_ADMIN' || r === 'ORG_MEMBER'

// ---------------------------------------------------------------- 사업·장비·회차

/** @type {S['Project'][]} */
const PROJECTS = [{ project_id: PRJ, name: '선박 영상·수질 실증 (합성)', status: 'ACTIVE' }]

/** @type {S['Device'][]} */
const DEVICES = [
  { device_id: DEV_PC, name: 'A 선박 PC', kind: 'COLLECTOR', device_type: 'AGENT_PC', project_id: PRJ, status: 'ACTIVE',
    collection_mode: 'C3_APPROVED_FULL', local_copy_policy: 'RETAINED', center_copy_may_be_only_copy: false,
    expected_interval_s: 60, last_heartbeat_at: at('05:58:00') },
  { device_id: DEV_CAM, name: 'A 선박 카메라', kind: 'SOURCE', device_type: 'CAMERA', collector_device_id: DEV_PC, project_id: PRJ,
    status: 'ACTIVE', collection_mode: 'C2_EVENT_CLIPS', local_copy_policy: 'ROTATING', local_retention_days: 7,
    center_copy_may_be_only_copy: true, expected_interval_s: 300, last_heartbeat_at: null },
  { device_id: DEV_SENSOR, name: 'A 수질 센서', kind: 'SOURCE', device_type: 'SENSOR', collector_device_id: DEV_PC, project_id: PRJ,
    status: 'ACTIVE', collection_mode: 'C3_APPROVED_FULL', local_copy_policy: 'RETAINED', center_copy_may_be_only_copy: false,
    expected_interval_s: 600, last_heartbeat_at: null },
]

/** @type {S['Session'][]} */
const SESSIONS = [
  { session_id: SES1, project_id: PRJ, label: '2026-11-03_항해01', title: '북항 시험 항해 (합성)', starts_at: at('00:00:00'),
    ends_at: at('06:00:00'), site: '시험 해역 1 (합성)', note: '합성 시험 회차', device_ids: [DEV_CAM, DEV_SENSOR],
    created_at: '2026-11-02T08:00:00Z' },
  { session_id: SES2, project_id: PRJ, label: '2026-11-05_항해02', title: '야간 항해 (합성)', starts_at: LATER, ends_at: null,
    site: null, note: null, device_ids: [DEV_CAM], created_at: '2026-11-04T08:00:00Z' },
]

// ---------------------------------------------------------------- 자산

/**
 * @param {Partial<Asset> & Pick<Asset, 'asset_id' | 'data_type' | 'variant' | 'source_system_id' | 'mime_type' | 'byte_size' | 'provenance'>} a
 * @returns {Asset}
 */
function asset(a) {
  return {
    schema_version: '1.0.0', organization_id: ORG_A, project_id: PRJ, session_id: SES1, data_role: 'BYPRODUCT',
    capture_start_at: null, ingested_at: at('06:10:00'), timestamp_quality: { source: 'UNKNOWN', verified: false },
    checksum_sha256: sha(a.byte_size), has_binary: true, collection_mode: 'C3_APPROVED_FULL', privacy_status: 'UNKNOWN',
    sensitivity_class: 'INTERNAL', sharing_scope: 'PRIVATE', retention_policy_id: 'raw-default', retention_until: '2031-11-03T00:00:00Z',
    quality_status: 'PASSED', quality_reasons: [], ingest_status: 'READY_PRIVATE', share_status: 'NONE', backup_class: 'NONE',
    ...a,
  }
}

const VIDEO_TIME = { capture_start_at: at('01:00:00'), capture_end_at: at('01:02:00'), timestamp_quality: /** @type {const} */ ({ source: 'NTP', verified: true }) }

/** @type {Asset[]} */
const ASSETS = [
  asset({ asset_id: AST_VIDEO, data_type: 'VIDEO', variant: 'ORIGINAL', source_system_id: DEV_CAM, mime_type: 'video/mp4', byte_size: 52_428_800,
    ...VIDEO_TIME, collection_mode: 'C2_EVENT_CLIPS', privacy_status: 'REVIEW_REQUIRED', share_status: 'CANDIDATE',
    provenance: { ingest_channel: 'AGENT', client_version: '0.1.0', original_filename: 'bridge-cam_20261103_100000.mp4',
      original_relative_path: '2026-11-03_항해01/bridge-cam_20261103_100000.mp4' },
    video_ext: { resolution: '1920x1080', fps: 30, duration_s: 120, codec: 'h264', container: 'mp4' },
    env_context: { joined_at: at('06:20:00'), radius_km: 20, window_min: 60, observations: [
      free({ metric: 'significant_wave_height', value: 0.8, unit: 'm', observed_at: at('01:00:00'), station_name: '합성 관측소 1',
        distance_km: 4.2, time_diff_s: 0, attribution: '합성 공공 관측 (모의 어댑터)' }),
      free({ metric: 'wind_speed', value: 5.1, unit: 'm/s', observed_at: at('01:00:00'), station_name: '합성 관측소 1',
        distance_km: 4.2, time_diff_s: 0, attribution: '합성 공공 관측 (모의 어댑터)' }),
    ] } }),
  asset({ asset_id: AST_PROXY, data_type: 'VIDEO', variant: 'PROXY', source_system_id: DEV_CAM, mime_type: 'video/webm', byte_size: 3_145_728,
    ...VIDEO_TIME, provenance: { ingest_channel: 'PIPELINE', parent_asset_ids: [AST_VIDEO], transform: 'PROXY', tool_version: 'ffmpeg 6.1' },
    video_ext: { resolution: '640x360', fps: 15, duration_s: 120, codec: 'vp9' } }),
  asset({ asset_id: AST_CLIP, data_type: 'VIDEO', variant: 'CLIP', source_system_id: DEV_CAM, mime_type: 'video/mp4', byte_size: 6_291_456,
    capture_start_at: at('01:00:10'), capture_end_at: at('01:00:25'), timestamp_quality: { source: 'NTP', verified: true },
    provenance: { ingest_channel: 'PIPELINE', parent_asset_ids: [AST_VIDEO], transform: 'CLIP', clip_range_s: [10, 25] },
    video_ext: { resolution: '1920x1080', fps: 30, duration_s: 15, codec: 'h264' } }),
  asset({ asset_id: AST_CSV, data_type: 'SENSOR', variant: 'ORIGINAL', source_system_id: DEV_SENSOR, mime_type: 'text/csv', byte_size: 48_213,
    capture_start_at: at('00:00:00'), capture_end_at: at('06:00:00'), timestamp_quality: { source: 'DEVICE', verified: false },
    sharing_scope: 'PROGRAM_REVIEW', privacy_status: 'NOT_APPLICABLE', quality_status: 'WARNING', quality_reasons: ['GAP_IN_SERIES'],
    provenance: { ingest_channel: 'AGENT', original_filename: 'water_20261103.csv', original_relative_path: '2026-11-03_항해01/water_20261103.csv' },
    sensor_ext: { metrics: ['sea_water_temperature', 'sea_water_salinity'], sampling_interval_s: 600 } }),
  asset({ asset_id: AST_IMAGE, data_type: 'IMAGE', variant: 'ORIGINAL', source_system_id: DEV_CAM, mime_type: 'image/jpeg', byte_size: 812_544,
    provenance: { ingest_channel: 'WEB', original_filename: 'deck_photo_unknown_time.jpg' } }),
  asset({ asset_id: AST_QUARANTINED, data_type: 'SENSOR', variant: 'ORIGINAL', source_system_id: DEV_SENSOR, mime_type: 'text/csv', byte_size: 20_480,
    session_id: null, ingest_status: 'QUARANTINED', quarantine_reason: 'UNKNOWN_SESSION', quality_status: 'PENDING', ingested_at: '2026-11-04T02:00:00Z',
    provenance: { ingest_channel: 'AGENT', original_filename: 'water_20261104.csv', original_relative_path: 'unsorted/water_20261104.csv' } }),
  asset({ asset_id: AST_RECEIVED, data_type: 'VIDEO', variant: 'ORIGINAL', source_system_id: DEV_CAM, mime_type: 'video/mp4', byte_size: 41_943_040,
    capture_start_at: at('05:30:00'), timestamp_quality: { source: 'DEVICE', verified: false }, ingest_status: 'RECEIVED', quality_status: 'PENDING',
    provenance: { ingest_channel: 'AGENT', original_filename: 'bridge-cam_20261103_143000.mp4' } }),
  asset({ asset_id: AST_B_QUARANTINED, organization_id: ORG_B, data_type: 'VIDEO', variant: 'ORIGINAL', source_system_id: id('dev', 5),
    mime_type: 'video/mp4', byte_size: 10_485_760, ingest_status: 'QUARANTINED', quarantine_reason: 'CHECKSUM_MISMATCH', quality_status: 'PENDING',
    session_id: null, ingested_at: '2026-11-04T05:00:00Z', provenance: { ingest_channel: 'API' } }),
  asset({ asset_id: AST_CANDIDATE, data_type: 'VIDEO', variant: 'ORIGINAL', source_system_id: DEV_CAM, mime_type: 'video/mp4', byte_size: 31_457_280,
    capture_start_at: at('02:10:00'), timestamp_quality: { source: 'NTP', verified: true }, share_status: 'CANDIDATE',
    provenance: { ingest_channel: 'AGENT', original_filename: 'bridge-cam_20261103_111000.mp4' } }),
  asset({ asset_id: AST_LEGAL, organization_id: ORG_B, data_type: 'VIDEO', variant: 'ORIGINAL', source_system_id: id('dev', 5), mime_type: 'video/mp4',
    byte_size: 20_971_520, share_status: 'LEGAL_REVIEW', session_id: null, provenance: { ingest_channel: 'AGENT', original_filename: 'beach-cctv_0007.mp4' } }),
  asset({ asset_id: AST_QUALITY, data_type: 'VIDEO', variant: 'ORIGINAL', source_system_id: DEV_CAM, mime_type: 'video/mp4', byte_size: 15_728_640,
    share_status: 'QUALITY_REVIEW', legal_basis_ref: '합성 법률 검토 2026-03', provenance: { ingest_channel: 'AGENT', original_filename: 'bridge-cam_20261103_120500.mp4' } }),
]

/** 자산 목록 조회: 역할별 범위(기관·운영자 전체·관리기관 L1) 다음 질의 조건 @param {Ctx} ctx */
function listAssets(ctx) {
  const scoped = ASSETS.filter((a) => (ctx.role === 'PLATFORM_OPERATOR' ? true
    : ctx.role === 'PROGRAM_VIEWER' ? a.sharing_scope === 'PROGRAM_REVIEW'
      : isOrg(ctx.role) && a.organization_id === ORG_A))
  const keys = /** @type {const} */ (['ingest_status', 'session_id', 'share_status', 'sharing_scope', 'variant', 'data_type', 'source_system_id', 'quality_status'])
  return scoped.filter((a) => keys.every((k) => !ctx.query.get(k) || a[k] === ctx.query.get(k)))
}

/** @param {string} assetId */
const findAsset = (assetId) => ASSETS.find((a) => a.asset_id === assetId)

// ---------------------------------------------------------------- 수집 상태·경보

/** @type {S['Incident'][]} */
const INCIDENTS = [
  { incident_id: id('inc', 1), organization_id: ORG_A, device_id: DEV_CAM, session_id: null, rule_code: 'HEARTBEAT_MISSING', severity: 'WARNING',
    status: 'OPEN', summary: 'A 선박 카메라를 맡은 수집기의 하트비트가 90분 동안 없습니다',
    guidance: '현장 PC 전원·네트워크를 확인하고 marine-agent status로 서비스 상태를 보세요', opened_at: at('04:30:00'),
    acknowledged_at: null, resolved_at: null, assignee: null, memo: null },
  { incident_id: id('inc', 2), organization_id: ORG_A, device_id: DEV_SENSOR, session_id: SES1, rule_code: 'OBSERVATION_LAG', severity: 'INFO',
    status: 'ACKNOWLEDGED', summary: 'A 수질 센서 관측값이 기대 주기보다 늦게 들어옵니다', guidance: '센서 기록 주기 설정을 확인하세요',
    opened_at: at('03:10:00'), acknowledged_at: at('03:20:00'), resolved_at: null, assignee: '현장 담당 (합성)', memo: '센서 재시작 예정' },
]
const INCIDENT_B = /** @type {S['Incident']} */ ({ incident_id: id('inc', 3), organization_id: ORG_B, device_id: DEV_B_PC, rule_code: 'UPLOAD_FAILURE_RATE',
  severity: 'CRITICAL', status: 'OPEN', summary: 'B 기관 업로드 실패율이 30%를 넘었습니다', guidance: '수집기 로그의 오류 코드를 확인하세요',
  opened_at: '2026-11-04T06:00:00Z' })

/** @type {S['AgentHealth'][]} */
const HEALTH_A = [
  { device_id: DEV_PC, organization_id: ORG_A, name: 'A 선박 PC', kind: 'COLLECTOR', status: 'OK', last_heartbeat_at: at('05:58:00'),
    last_registered_at: at('05:50:00'), expected_interval_s: 60, lag_s: 600, heartbeat_lag_s: 120, queue_items: 2, queue_bytes: 4_194_304,
    last_error_code: null, open_alerts: 0, clock_offset_ms: 120, policy_mismatch: false },
  { device_id: DEV_CAM, organization_id: ORG_A, name: 'A 선박 카메라', kind: 'SOURCE', status: 'STALE', last_heartbeat_at: null,
    last_registered_at: at('04:00:00'), expected_interval_s: 300, lag_s: 7200, heartbeat_lag_s: null, queue_items: 0,
    last_error_code: 'UPLOAD_TIMEOUT', open_alerts: 1, policy_mismatch: true },
  { device_id: DEV_SENSOR, organization_id: ORG_A, name: 'A 수질 센서', kind: 'SOURCE', status: 'OK', last_heartbeat_at: null,
    last_observation_at: at('05:50:00'), expected_interval_s: 600, lag_s: 600, queue_items: 0, last_error_code: null, open_alerts: 0 },
]
/** @type {S['AgentHealth']} */
const HEALTH_B = { device_id: DEV_B_PC, organization_id: ORG_B, name: 'B 해수욕장 NVR PC', kind: 'COLLECTOR', status: 'NEVER_SEEN',
  last_heartbeat_at: null, last_registered_at: null, queue_items: 0, last_error_code: null, open_alerts: 1 }

// ---------------------------------------------------------------- 사용량·운영

/** @type {S['OrgUsage'][]} */
const USAGE = [
  { organization_id: ORG_A, quota_bytes: 1_099_511_627_776, usage_bytes: 268_435_456_000, usage_ratio: 0.2441,
    breakdown: { stored_bytes: 214_748_364_800, quarantine_bytes: 1_073_741_824, reserved_bytes: 2_147_483_648, backup_bytes: 50_465_865_728 } },
  { organization_id: ORG_B, quota_bytes: 549_755_813_888, usage_bytes: 511_272_607_744, usage_ratio: 0.93,
    breakdown: { stored_bytes: 509_101_604_864, quarantine_bytes: 1_073_741_824, reserved_bytes: 1_097_261_056, backup_bytes: 0 } },
]

/** @type {S['Agreement'][]} */
const AGREEMENTS = [
  { agreement_id: id('agr', 1), organization_id: ORG_A, tier: 'TIER2', reference: '합성 약정 2026-001', valid_from: '2026-10-01T00:00:00Z',
    valid_until: null, allowed_purposes: ['AI_TRAINING', 'JOINT_RESEARCH'], status: 'ACTIVE', in_effect: true, created_by: USR_OPERATOR },
  { agreement_id: id('agr', 2), organization_id: ORG_A, tier: 'TIER1', reference: '합성 약정 2025-014', valid_from: '2025-10-01T00:00:00Z',
    valid_until: '2026-09-30T15:00:00Z', allowed_purposes: [], status: 'ENDED', in_effect: false, created_by: USR_OPERATOR },
]

/** @type {S['BreakGlassGrant'][]} */
const GRANTS_BREAK_GLASS = [
  { grant_id: id('brk', 1), organization_id: ORG_A, target_type: 'ASSET', target_id: AST_VIDEO, reason: '장애 INC-합성-001 원본 손상 여부 확인',
    duration_s: 3600, status: 'EXPIRED', requested_by: USR_OPERATOR, approved_by: USR_OPERATOR2, starts_at: '2026-11-04T01:00:00Z',
    expires_at: '2026-11-04T02:00:00Z', created_at: '2026-11-04T00:50:00Z' },
]
/** @type {S['BreakGlassGrant']} */
const BREAK_GLASS_REQUESTED = { grant_id: id('brk', 2), organization_id: ORG_B, target_type: 'SESSION', target_id: id('ses', 9),
  reason: '장애 INC-합성-003 회차 자료 무결성 확인', duration_s: 7200, status: 'REQUESTED', requested_by: USR_OPERATOR2,
  approved_by: null, starts_at: null, expires_at: null, created_at: '2026-11-04T07:00:00Z' }

// ---------------------------------------------------------------- 실증 이벤트·관측값

/** @type {S['EventRecord'][]} */
const EVENTS = [
  { event_id: EVT_VESSEL, organization_id: ORG_A, schema_version: '1.0.0', session_id: SES1, source_system_id: DEV_CAM, event_code: 'VESSEL.small_boat',
    started_at: at('01:00:30'), ended_at: at('01:01:10'), timestamp_quality: { source: 'NTP', verified: true }, severity: 'INFO', source: 'MODEL',
    model_ref: 'detector-mock@0.1.0', confidence: 0.87, human_verified: false, linked_asset_ids: [AST_VIDEO], created_at: at('01:05:00') },
  { event_id: EVT_FLOAT, organization_id: ORG_A, schema_version: '1.0.0', session_id: SES1, event_code: 'FLOATING_OBJECT', started_at: at('02:10:00'),
    timestamp_quality: { source: 'MANUAL', verified: false }, source: 'MANUAL', human_verified: true, note: '부유물 육안 확인 (합성)',
    created_at: at('02:15:00') },
  { event_id: EVT_FAULT, organization_id: ORG_A, schema_version: '1.0.0', session_id: SES1, source_system_id: DEV_SENSOR, event_code: 'EQUIPMENT_FAULT',
    started_at: at('03:00:00'), ended_at: at('03:20:00'), timestamp_quality: { source: 'DEVICE', verified: false }, severity: 'CRITICAL',
    source: 'DEVICE', human_verified: true, created_at: at('03:21:00') },
]

const METRICS = [{ metric: 'sea_water_temperature', unit: 'degC', base: 17 }, { metric: 'sea_water_salinity', unit: 'PSU', base: 33 }]
const OBS_FROM = Date.parse(at('00:00:00'))
const OBS_TO = Date.parse(at('06:00:00'))
const GAP = [Date.parse(at('03:00:00')), Date.parse(at('03:30:00'))] // 센서 장애 구간: 결측만 기록됨

/** 관측값 조회: interval이 있으면 구간 집계, 없으면 표본 목록 @param {Ctx} ctx @returns {Get<'/v1/observations'>} */
function observations(ctx) {
  const device = ctx.query.get('source_system_id')
  const metric = ctx.query.get('metric')
  const from = Math.max(Date.parse(ctx.query.get('from') ?? '') || OBS_FROM, OBS_FROM)
  const to = Math.min(Date.parse(ctx.query.get('to') ?? '') || OBS_TO, OBS_TO)
  const ours = !device || device === DEV_SENSOR
  const interval = ctx.query.get('interval')
  if (interval) {
    const m = /^PT(\d+)([MH])$/.exec(interval)
    const step = m ? Number(m[1]) * (m[2] === 'H' ? 3_600_000 : 60_000) : 600_000
    const base = METRICS.find((x) => x.metric === metric)?.base ?? 0
    const items = []
    for (let t = from; ours && base && t < to; t += step) {
      const missing = t >= GAP[0] && t < GAP[1]
      items.push({ bucket_start: new Date(t).toISOString().replace(/\.\d+Z$/, 'Z'), count: 1, missing_count: missing ? 1 : 0,
        min: missing ? null : base, max: missing ? null : base + 0.4, avg: missing ? null : base + 0.2 })
    }
    return { interval, items }
  }
  /** @type {S['ObservationRecord'][]} */
  const items = []
  for (const x of METRICS)
    for (let t = from; ours && t < to; t += 3_600_000)
      items.push({ source_system_id: DEV_SENSOR, metric: x.metric, unit: x.unit, value: x.base + 0.2, quality_flag: 'GOOD',
        observed_at: new Date(t).toISOString().replace(/\.\d+Z$/, 'Z'), source_asset_id: AST_CSV })
  return { items, next_cursor: null }
}

// ---------------------------------------------------------------- 회차 기록·보고서

/** @type {S['SessionRecord'][]} */
const RECORDS = [
  { record_id: REC1, session_id: SES1, kind: 'LOG', title: '1차 항해 실증일지', conditions: '맑음, 유의파고 0.8 m (합성)', result: '정상 수집',
    evidence_refs: [{ kind: 'ASSET', asset_id: AST_VIDEO }, { kind: 'EVENT', event_id: EVT_VESSEL }], created_at: at('07:00:00'),
    created_by: USR_ADMIN, version: 2, latest_version: 2, status: 'APPROVED', approved_by: USR_ADMIN, approved_at: at('08:00:00'),
    evidence_damaged: false },
  { record_id: REC2, session_id: SES1, kind: 'TEST', title: '소형 선박 탐지 시험', test_item: '소형 선박 탐지율', result: '검증 필요',
    evidence_refs: [{ kind: 'ASSET', asset_id: AST_RECEIVED }], created_at: at('07:30:00'), created_by: USR_MEMBER, version: 1,
    status: 'DRAFT', evidence_damaged: true, damage: [{ asset_id: AST_RECEIVED, reason: 'QUARANTINED', detected_at: at('09:00:00') }] },
]

/** @type {S['MetricResult'][]} */
const METRIC_RESULTS = [
  { metric_code: 'small_vessel_events', metric_version: 1, name: '소형 선박 이벤트 수', unit: '건', status: 'NEEDS_VERIFICATION', value: 1,
    formula: 'count(event_code = VESSEL.small_boat)', input_count: 1, missing_count: 0, sources: { event_ids: [EVT_VESSEL] } },
  { metric_code: 'mean_water_temperature', metric_version: 1, name: '평균 수온', unit: 'degC', status: 'OK', value: 17.2,
    formula: 'mean(sea_water_temperature)', input_count: 33, missing_count: 3,
    sources: { observation_ranges: [free({ source_system_id: DEV_SENSOR, metric: 'sea_water_temperature', from: at('00:00:00'), to: at('06:00:00'), count: 36 })] } },
  { metric_code: 'video_hours', metric_version: 1, name: '영상 시간', unit: 'h', status: 'NO_DATA', value: null, formula: 'sum(duration_s) / 3600',
    input_count: 0, missing_count: 0, sources: {} },
]

/** @param {string} report_id @param {S['report_status']} status @param {Partial<S['Report']>} extra @returns {S['Report']} */
const report = (report_id, status, extra) => ({
  report_id, organization_id: ORG_A, project_id: PRJ, session_id: SES1, title: '2026-11-03_항해01 회차 보고서 (합성)', template_code: 'session_basic',
  template_version: 1, version: 1, latest_version: 1, status, generated_at: at('09:00:00'), data_as_of: at('08:55:00'),
  fields: [
    { key: 'summary', label: '요약', auto_value: '자산 5건 · 이벤트 3건', value: '자산 5건 · 이벤트 3건 (합성 회차)', edited: true,
      history: [{ previous: null, value: '자산 5건 · 이벤트 3건 (합성 회차)', edited_by: USR_ADMIN, edited_at: at('09:10:00') }] },
    { key: 'weather', label: '기상 조건', auto_value: '유의파고 0.8 m', value: '유의파고 0.8 m', edited: false },
  ],
  metrics: METRIC_RESULTS, record_ids: [REC1], evidence_damaged: false, created_at: at('09:00:00'), ...extra,
})

const REPORTS = [
  report(RPT_DRAFT, 'DRAFT', { title: '2026-11-03_항해01 회차 보고서 v2 (합성)', version: 2, latest_version: 2, evidence_damaged: true,
    damage: [{ asset_id: AST_RECEIVED, reason: 'QUARANTINED', detected_at: at('09:30:00') }] }),
  report(RPT_APPROVED, 'APPROVED', { approved_by: USR_ADMIN, approved_at: at('09:20:00'), snapshot_sha256: sha(7) }),
  report(RPT_SUBMITTED, 'SUBMITTED', { title: '2026-11-01 사전 점검 보고서 (합성)', approved_by: USR_ADMIN, approved_at: '2026-11-01T09:00:00Z',
    submitted_at: '2026-11-01T10:00:00Z', snapshot_sha256: sha(8), evidence_damaged: true, damage: [] }),
]
/** 관리기관 열람자는 제출된 보고서만 @param {Role} role */
const visibleReports = (role) => REPORTS.filter((r) => role !== 'PROGRAM_VIEWER' || r.status === 'SUBMITTED')

/** @type {S['ReportTemplate'][]} */
const TEMPLATES = [
  { code: 'session_basic', name: '회차 기본 보고서 (범용)', scope: 'SESSION', version: 1, has_hwpx: false, created_at: '2026-10-01T00:00:00Z',
    sections: [{ kind: 'TEXT', title: '개요', text: '{{session.label}} 회차' }, { kind: 'METRICS', title: '지표', metrics: ['small_vessel_events'] }] },
]

// ---------------------------------------------------------------- 공유·데이터셋·이용

/** @type {S['data_card']} */
const DATA_CARD = {
  title: '항만 부유물 비식별 영상 (합성)', rights_holders: ['제공기관 A (합성)'], review_owner: '운영기관 검수팀 (합성)',
  source_summary: { organization_count: 1, session_count: 1, device_types: ['CAMERA'], period_start: '2026-11-03', period_end: '2026-11-03',
    sea_areas_generalized: ['시험 해역 1'] },
  contents: { file_count: 3, total_bytes: 734_003_200, formats: ['video/mp4'] },
  conditions: { missing_data_note: '야간 자료 없음', bias_note: '맑은 날 위주', limitations: '시험용 합성 자료' },
  privacy: { risk_note: '얼굴·번호판 가림 처리', processing_methods: ['BLUR'], review_method: '표본 프레임 원본 대비 검수' },
  lineage: { model_versions: ['deid-baseline@0.1.0'] },
  usage: { allowed_purposes: ['AI_TRAINING'], redistribution_allowed: false, commercial_use_allowed: false, revocation_terms: '철회 시 30일 안 파기' },
  delivery: { methods: ['DOWNLOAD_URL'] },
}

/** 이용기관에는 게시된 데이터셋의 다 만든 버전만 보인다 @param {Ctx} ctx @returns {S['Dataset'][]} */
function datasets(ctx) {
  const ready = { dataset_version_id: DSV1, version: '1.0.0', created_at: '2026-11-04T00:00:00Z', status: /** @type {const} */ ('READY'), build_error: null,
    scope: /** @type {const} */ ('APPROVED_PARTNERS'), item_count: 3, total_bytes: 734_003_200, manifest_sha256: sha(10) }
  const building = { dataset_version_id: DSV2, version: '1.1.0', created_at: '2026-11-05T00:00:00Z', status: /** @type {const} */ ('BUILDING'), build_error: null,
    scope: /** @type {const} */ ('APPROVED_PARTNERS'), item_count: 1200, total_bytes: 0, manifest_sha256: null }
  const published = { dataset_id: DST1, title: '항만 부유물 비식별 영상 (합성)', published: true, published_at: '2026-11-04T01:00:00Z',
    data_card: DATA_CARD, samples: [{ url: ctx.media.image, expires_at: LATER }] }
  if (ctx.role === 'CONSUMER') return [{ ...published, versions: [ready] }]
  return [{ ...published, versions: [ready, building] },
    { dataset_id: DST2, title: '수질 관측 묶음 (합성)', published: false, published_at: null, versions: [] }]
}

/** @type {S['AccessRequest'][]} */
const ACCESS_REQUESTS = [
  { access_request_id: ACR1, dataset_version_id: DSV1, purpose: 'AI_TRAINING', status: 'REQUESTED', pending_provider_approvals: 1,
    deadline_at: '2026-11-19T00:00:00Z', created_at: '2026-11-05T00:00:00Z' },
  { access_request_id: ACR2, dataset_version_id: DSV1, purpose: 'JOINT_RESEARCH', status: 'PROVIDER_APPROVED', pending_provider_approvals: 0,
    deadline_at: '2026-11-18T00:00:00Z', created_at: '2026-11-04T00:00:00Z' },
  { access_request_id: ACR3, dataset_version_id: DSV1, purpose: 'MODEL_IMPROVEMENT', status: 'GRANTED', grant_id: GRT1, grant_status: 'GRANTED',
    grant_expires_at: '2027-05-01T00:00:00Z', deadline_at: '2026-11-10T00:00:00Z', created_at: '2026-10-27T00:00:00Z' },
]

/** @type {S['Grant'][]} */
const GRANTS = [
  { grant_id: GRT1, access_request_id: ACR3, dataset_version_id: DSV1, organization_id: ORG_CONSUMER, status: 'GRANTED',
    starts_at: '2026-11-01T00:00:00Z', expires_at: '2027-05-01T00:00:00Z', destruction_confirmed_at: null },
  { grant_id: GRT2, dataset_version_id: DSV1, organization_id: ORG_CONSUMER, status: 'REVOKED', starts_at: '2026-10-01T00:00:00Z',
    expires_at: '2027-04-01T00:00:00Z', revoked_at: '2026-11-02T00:00:00Z', revoke_reason: '제공기관 철회 (합성)',
    notice: '이미 받은 파일은 회수되지 않습니다. 보관 사본을 파기한 뒤 파기 확인을 제출하세요', destruction_confirmed_at: null },
]

/** @type {S['DeidReview'][]} */
const DEID_REVIEWS = [
  { review_id: RVW1, organization_id: ORG_A, session_id: SES1, original_asset_id: AST_VIDEO, deid_asset_id: AST_DEID, reviewer_id: USR_REVIEWER,
    risk_level: 'HIGH', samples: [0.5, 1.0, 1.5], recheck: true, status: 'ASSIGNED', created_at: '2026-11-04T03:00:00Z',
    processing: free({ model: { name: 'deid-baseline', version: '0.1.0' }, params: { method: 'gaussian_blur' }, detections: { FACE: 2, LICENSE_PLATE: 0 },
      note: '처리 이력입니다. 자동 처리만으로 적정성을 보증하지 않습니다' }) },
]

// ---------------------------------------------------------------- 응답표 (계약 경로 → 응답)

/** 생성 타입에 섞여 든 $defs(스키마 정의 묶음)는 실제 값이 아니라서 뺀 모양으로 검사한다
 * @param {Omit<S['integration-card.schema'], '$defs'>} content @returns {S['integration-card.schema']} */
const cardContent = (content) => /** @type {S['integration-card.schema']} */ (content)

/** @type {GetRoutes} */
const GET = {
  '/v1/me': ({ role }) => {
    const p = PRINCIPALS[role]
    if (!p) throw new Error(`합성 사용자 없음: ${role}`)
    return { principal_kind: 'USER', user_id: p.user, organization_id: p.org, org_type: p.org_type, roles: [role], mfa: p.mfa }
  },
  '/v1/system/capabilities': () => ({ api_version: '1.0', schema_versions: ['1.0.0'], gpu_enabled: false,
    processing_steps: ['META', 'PROXY', 'THUMBNAIL', 'SENSOR_PARSE'], report_formats: ['PDF', 'XLSX'], external_sources: ['mock-ocean'],
    features: { break_glass: true, webhooks: true, sharing: true } }),
  '/v1/projects': () => ({ items: PROJECTS, next_cursor: null }),
  '/v1/sessions': () => ({ items: SESSIONS, next_cursor: null }),
  '/v1/sessions/{session_id}': ({ params }) => SESSIONS.find((s) => s.session_id === params.session_id) ?? notFound(),
  '/v1/sessions/{session_id}/records': ({ params }) => ({ items: RECORDS.filter((r) => r.session_id === params.session_id) }),
  '/v1/devices': () => ({ items: DEVICES, next_cursor: null }),
  '/v1/assets': (ctx) => ({ items: listAssets(ctx), next_cursor: null }),
  '/v1/assets/{asset_id}': ({ params }) => findAsset(params.asset_id) ?? notFound(),
  '/v1/assets/{asset_id}/lineage': ({ params }) => ({ asset_id: params.asset_id, ancestors: [],
    descendants: params.asset_id === AST_VIDEO ? [
      { asset_id: AST_PROXY, relation: 'PROXY', tool_version: 'ffmpeg 6.1', created_at: at('06:30:00') },
      { asset_id: AST_CLIP, relation: 'CLIP', tool_version: 'ffmpeg 6.1', created_at: at('07:10:00') },
    ] : [] }),
  '/v1/assets/{asset_id}/share-review': ({ params }) => ({ asset_id: params.asset_id, share_status: findAsset(params.asset_id)?.share_status ?? 'NONE',
    legal_basis_ref: null, max_scope: 'PROGRAM_REVIEW', derived: [],
    events: [{ from_status: 'NONE', to_status: 'CANDIDATE', actor: USR_ADMIN, reason: '부산물 영상 공유 후보 지정', at: at('08:30:00') }] }),
  '/v1/assets/{asset_id}/clips': ({ params }) => ({ items: params.asset_id !== AST_VIDEO ? [] : [
    { job_id: id('job', 3), asset_id: AST_VIDEO, start_s: 40, end_s: 55.5, status: 'QUEUED', requested_at: at('09:40:00') },
    { job_id: id('job', 2), asset_id: AST_VIDEO, start_s: 0, end_s: 1, status: 'FAILED', requested_at: at('08:00:00'), error_code: 'CLIP_ENCODE_FAILED' },
    { job_id: id('job', 1), asset_id: AST_VIDEO, start_s: 10, end_s: 25, status: 'DONE', requested_at: at('07:00:00'), clip_asset_id: AST_CLIP },
  ] }),
  '/v1/events': ({ query }) => ({ items: EVENTS.filter((e) => !query.get('session_id') || e.session_id === query.get('session_id')), next_cursor: null }),
  '/v1/events/{event_id}/overlapping-assets': ({ params, query }) => {
    const margin = Number(query.get('margin_s') ?? '0')
    const cursor = query.get('cursor')
    const next = `synthetic-overlap-${margin}`
    if (cursor !== null && cursor !== next) return notFound()
    const overlap = margin > 0 ? 120 : 40
    const items = cursor === null
      ? [{ asset: ASSETS[0], overlap_s: overlap, linked: true }]
      : [{ asset: ASSETS[1], overlap_s: overlap, linked: false }]
    return { event_id: params.event_id, session_id: SES1, items, unknown_capture_count: 1,
      window: { from: new Date(Date.parse(at('01:00:30')) - margin * 1000).toISOString(),
        to: new Date(Date.parse(at('01:01:10')) + margin * 1000).toISOString() },
      next_cursor: cursor === null ? next : null }
  },
  '/v1/observations': observations,
  '/v1/usage': ({ role }) => ({ items: role === 'PLATFORM_OPERATOR' ? USAGE : USAGE.slice(0, 1) }),
  '/v1/ingest-status': ({ role }) => ({ generated_at: at('06:00:00'),
    devices: role === 'PLATFORM_OPERATOR' ? [...HEALTH_A, HEALTH_B] : HEALTH_A,
    open_incidents: role === 'PLATFORM_OPERATOR' ? [...INCIDENTS, INCIDENT_B] : INCIDENTS,
    top_failure_reasons: [{ source: 'QUARANTINE', code: 'UNKNOWN_SESSION', count: 3 }, { source: 'UPLOAD', code: 'CHECKSUM_MISMATCH', count: 1 }] }),
  '/v1/reports': ({ role }) => ({ items: visibleReports(role), next_cursor: null }),
  '/v1/reports/{report_id}': ({ role, params }) => visibleReports(role).find((r) => r.report_id === params.report_id) ?? notFound(),
  '/v1/reports/{report_id}/metrics/{metric_code}/trace': ({ params }) => ({
    metric: METRIC_RESULTS.find((m) => m.metric_code === params.metric_code) ?? notFound(),
    records: [{ record_id: REC1, version: 2, title: '1차 항해 실증일지' }],
    events: [{ event_id: EVT_VESSEL, event_code: 'VESSEL.small_boat', source: 'MODEL', human_verified: false, asset_ids: [AST_VIDEO] }],
    observation_ranges: [free({ source_system_id: DEV_SENSOR, metric: 'sea_water_temperature', from: at('00:00:00'), to: at('06:00:00'), count: 36 })],
    assets: [{ asset_id: AST_CLIP, variant: 'CLIP', ingest_status: 'READY_PRIVATE', originals: [AST_VIDEO] }], hidden_count: 1 }),
  '/v1/report-templates': () => ({ items: TEMPLATES }),
  '/v1/access-requests': ({ role }) => ({ items: role === 'PLATFORM_OPERATOR' ? ACCESS_REQUESTS.filter((r) => r.status === 'PROVIDER_APPROVED')
    : role === 'CONSUMER' ? ACCESS_REQUESTS.slice(1) : ACCESS_REQUESTS }),
  '/v1/grants': () => ({ items: GRANTS }),
  '/v1/usage-history': () => ({ items: [
    { kind: 'REQUESTED', at: '2026-10-27T00:00:00Z', dataset_version_id: DSV1, access_request_id: ACR3, consumer_organization: '이용기관 C (합성)', purpose: 'MODEL_IMPROVEMENT' },
    { kind: 'GRANTED', at: '2026-11-01T00:00:00Z', dataset_version_id: DSV1, access_request_id: ACR3, consumer_organization: '이용기관 C (합성)', purpose: 'MODEL_IMPROVEMENT' },
    { kind: 'DOWNLOAD_ISSUED', at: '2026-11-02T00:00:00Z', dataset_version_id: DSV1, access_request_id: ACR3, consumer_organization: '이용기관 C (합성)', detail: '파일 3개' },
  ] }),
  '/v1/datasets': (ctx) => ({ items: datasets(ctx), next_cursor: null }),
  '/v1/datasets/{dataset_id}/versions/{dataset_version_id}/lineage': ({ params }) => ({ dataset_id: params.dataset_id,
    dataset_version_id: params.dataset_version_id, version: '1.0.0', manifest_sha256: sha(10), hidden_item_count: 1,
    items: [{ asset_id: AST_DEID, path: 'video/0001.mp4', organization_id: ORG_A, source_asset_id: AST_CANDIDATE,
      chain: [{ asset_id: AST_DEID, relation: 'DEID', variant: 'DEID' }, { asset_id: AST_CANDIDATE, relation: 'ORIGINAL', variant: 'ORIGINAL' }],
      processing: { model: { name: 'deid-baseline', version: '0.1.0', sha256: null, license: 'Apache-2.0' }, processed_at: '2026-11-03T12:00:00Z' },
      reviews: [{ review_id: RVW1, reviewer_id: USR_REVIEWER, status: 'PASSED', decided_at: '2026-11-03T13:00:00Z' }], review_result: 'PASSED' }] }),
  '/v1/deid-reviews': () => ({ items: DEID_REVIEWS }),
  '/v1/deid-quality': () => ({ reviews: 12, reprocess_ratio: 0.083, note: '검수 결과 집계입니다. 적정성 보증이 아닙니다',
    classes: [{ class: 'FACE', detected: 40, missed: 2, miss_rate: 0.05 }, { class: 'LICENSE_PLATE', detected: 9, missed: 0, miss_rate: 0 }] }),
  '/v1/integration-cards': () => ({ items: [{ integration_card_id: id('icd', 1), version: 2, organization_id: ORG_A, device_id: DEV_CAM,
    created_by: USR_ADMIN, created_at: '2026-10-20T00:00:00Z', linked_policies: [{ device_id: DEV_CAM, version: 1 }],
    content: cardContent({ basic: { project_name: '선박 영상 실증 (합성)', start_month: '2026-11' }, current_storage: { local_copy_policy: 'ROTATING', note: 'NVR 7일 순환 (합성)' },
      collection_mode_requested: 'C2_EVENT_CLIPS', integration_methods: ['폴더 에이전트'], desired_services: ['영상 검색', '보고서 초안'] }),
    // 09번 작업 3: 확인 필요 분야와 난이도(기준 미정 — 등급을 매기지 않는다, ADR-0037)
    open_items: [{ section: 'time_sync', label: '시각', state: 'EMPTY', pointers: [] },
      { section: 'current_storage', label: '현행 저장', state: 'NEEDS_CONFIRMATION', pointers: ['/current_storage/note'] }],
    difficulty_grade: { grade: null, criteria_defined: false, note: '기준 미정 — 확인 필요' } }] }),
  '/v1/policy-drafts': () => ({ items: [], next_cursor: null }),
  '/v1/webhooks/subscriptions': () => ({ items: [
    { webhook_id: id('whk', 1), url: 'https://hooks.example.org/marine', event_types: ['upload.registered', 'upload.quarantined'], active: true, created_at: '2026-10-21T00:00:00Z' },
    { webhook_id: id('whk', 2), url: 'https://old.example.org/hook', event_types: ['grant.revoked'], active: false, created_at: '2026-10-01T00:00:00Z',
      disabled_at: '2026-11-02T00:00:00Z', disabled_reason: '8회 연속 실패 (마지막 응답 500)' },
  ] }),
  '/v1/agreements': () => ({ items: AGREEMENTS }),
  '/v1/break-glass-grants': ({ role }) => ({ items: role === 'PLATFORM_OPERATOR' ? [BREAK_GLASS_REQUESTED, ...GRANTS_BREAK_GLASS] : GRANTS_BREAK_GLASS }),
  '/v1/usage-reports': ({ query }) => ({ month: query.get('month') ?? '2026-11', items: [{ organization_id: ORG_A, snapshot_day: '2026-11-04',
    storage_by_zone: { RAW: 3_221_225_472, PROCESSED: 2_147_483_648, EVIDENCE: 0, DATASET: 0, QUARANTINE: 1_048_576 }, storage_by_tier: { HOT: 5_368_709_120, WARM: 0 },
    backup_target_bytes: 2_147_483_648, requested_backup_bytes: 1_073_741_824, processing_seconds: { CPU: 5400, GPU: 0 },
    downloads: { url_count: 3, url_bytes: 3_221_225_472, distribution_count: 1 } }] }),
  '/v1/audit-logs': () => ({ next_cursor: null, items: [
    { audit_id: id('aud', 1), occurred_at: at('09:00:00'), actor_kind: 'USER', actor_id: USR_ADMIN, action: 'ASSET_VIEWED', target_type: 'Asset', target_id: AST_VIDEO, result: 'SUCCESS' },
    { audit_id: id('aud', 2), occurred_at: at('09:05:00'), actor_kind: 'USER', actor_id: USR_MEMBER, action: 'DOWNLOAD_URL_ISSUED', target_type: 'Asset', target_id: AST_PROXY, result: 'SUCCESS' },
    { audit_id: id('aud', 3), occurred_at: at('09:06:00'), actor_kind: 'USER', actor_id: id('usr', 9), action: 'CROSS_ORG_ACCESS', target_type: 'Asset', target_id: AST_B_QUARANTINED, result: 'DENIED' },
  ] }),
  // 운영 콘솔 정책 편집 (09번 작업 13, ADR-0038)
  '/v1/organization-policies': () => ({ items: [
    { organization_id: ORG_A, name: '제공기관 A (합성)', org_type: 'PROVIDER', quota_bytes: 1_099_511_627_776, usage_bytes: 779_708_063_744,
      malware_scan_required: false, over_quota: false },
    { organization_id: ORG_B, name: '제공기관 B (합성)', org_type: 'PROVIDER', quota_bytes: 107_374_182_400, usage_bytes: 118_111_600_640,
      malware_scan_required: true, over_quota: true }] }),
  '/v1/retention-policies': () => ({ items: [
    { target: 'RAW', policy_id: 'raw_default', retention_days: 3650, expiry_action: 'DELETE', notify_days_before: 30, updated_at: LATER, action_locked: false },
    { target: 'EVIDENCE', policy_id: 'evidence_keep', retention_days: null, expiry_action: 'KEEP', notify_days_before: 30, updated_at: LATER, action_locked: true },
    { target: 'QUARANTINE:UNKNOWN_SESSION', policy_id: 'quarantine_unknown_session', retention_days: null, expiry_action: 'KEEP',
      notify_days_before: 30, updated_at: LATER, action_locked: true }] }),
  '/v1/job-policies': () => ({ items: [
    { job_type: 'integrity_verify', priority: 100, resource_class: 'CPU', gpu_fallback: 'CPU', max_attempts: 5, lease_s: 3600, queued_jobs: 0 },
    { job_type: 'deid', priority: 90, resource_class: 'GPU', gpu_fallback: 'CPU', max_attempts: 3, lease_s: 7200, queued_jobs: 12 }] }),
  '/v1/capacity-forecast': () => ({ capacity_bytes: 1_099_511_627_776, threshold_ratio: 0.7, lead_time_days: 180, window_days: 30, current_bytes: 779_708_063_744,
    current_ratio: 0.709, slope_bytes_per_day: 10_737_418_240, reach_date: '2026-11-05', within_lead_time: true, note: null,
    history: [{ day: '2026-11-03', total_bytes: 758_233_227_264 }, { day: '2026-11-04', total_bytes: 768_970_645_504 }] }),
  '/v1/backup-runs': () => ({ items: [
    { backup_run_id: id('bkr', 2), status: 'FAILED', started_at: '2026-11-05T17:00:00Z', finished_at: '2026-11-05T17:02:00Z', object_count: 1520,
      copied_count: 0, skipped_count: 0, total_bytes: 214_748_364_800, copied_bytes: 0, error: '백업 저장소에 연결하지 못했습니다 (합성)', restore_test: null },
    { backup_run_id: id('bkr', 1), status: 'SUCCEEDED', started_at: '2026-11-04T17:00:00Z', finished_at: '2026-11-04T17:40:00Z', object_count: 1500,
      copied_count: 37, skipped_count: 1463, total_bytes: 212_600_881_152, copied_bytes: 1_073_741_824, error: null,
      restore_test: { tested_at: '2026-11-04T18:00:00Z', ok: true, tables_checked: 42, objects_checked: 20, audit_chain_ok: true } },
  ] }),
  '/v1/models': () => ({ allowed_licenses: ['Apache-2.0', 'MIT', 'BSD-3-Clause'], items: [
    { name: 'deid-baseline', version: '0.1.0', sha256: sha(9), license: 'Apache-2.0', weights_uri: 'models/deid-baseline-0.1.0.onnx',
      training_data_terms: '합성 데이터로만 학습 (시험용)', approved_by: USR_OPERATOR, loadable: true, created_at: '2026-10-15T00:00:00Z' },
  ] }),
  '/v1/watch-rules': () => ({ items: [
    { rule_code: 'HEARTBEAT_MISSING', description: '하트비트 무응답', enabled: true, severity: 'WARNING', params: free({ multiplier: 3, min_s: 300 }),
      notify_org: true, notify_operator: true, channels: ['EMAIL'], realert_interval_s: 3600, data_source_ready: true },
    { rule_code: 'STORAGE_FORECAST', description: '용량 예측', enabled: true, severity: 'CRITICAL', params: free({ threshold_ratio: 0.7, lead_time_days: 180 }),
      notify_org: false, notify_operator: true, channels: ['EMAIL', 'WEBHOOK'], realert_interval_s: 86400, data_source_ready: true },
    { rule_code: 'OPENAPI_FAILURE', description: 'Open API 실패', enabled: false, severity: 'INFO', params: free({ consecutive: 3 }),
      notify_org: false, notify_operator: true, channels: ['EMAIL'], realert_interval_s: 3600, data_source_ready: false },
  ] }),
  '/v1/inspections': () => ({ next_cursor: null, items: [
    { inspection_id: id('isp', 1), cycle: 'DAILY', period_key: '2026-11-03', status: 'COMPLETED', due_at: '2026-11-04T09:00:00Z', created_at: '2026-11-04T00:00:00Z',
      completed_at: '2026-11-04T02:00:00Z', completed_by: USR_OPERATOR, evidence: [],
      items: [{ item_code: 'agents_ok', title: '에이전트 하트비트 정상', auto: true, result: 'PASS' }] },
    { inspection_id: id('isp', 2), cycle: 'DAILY', period_key: '2026-11-04', status: 'IN_PROGRESS', due_at: '2026-11-05T09:00:00Z', created_at: '2026-11-05T00:00:00Z',
      evidence: [], items: [
        { item_code: 'agents_ok', title: '에이전트 하트비트 정상', auto: true, result: 'FAIL' },
        { item_code: 'backup_ok', title: '백업 성공', auto: true, result: 'AUTO_UNAVAILABLE' },
        { item_code: 'room_visual', title: '서버실 육안 점검', auto: false, result: null },
      ] },
  ] }),
  '/v1/external-sources': () => ({ items: [{ source_id: 'mock-ocean', name: '합성 해양 관측 (모의 어댑터)', adapter_kind: 'MOCK', endpoint: null,
    secret_ref: null, interval_s: 3600, max_window_s: 86400, supports_backfill: true,
    stations: [{ code: 'S001', name: '합성 관측소 1', lat: 34.5, lon: 128.5 }],
    field_mapping: { station_path: 'station', time_path: 'time', metrics: { wind_speed: { path: 'wind', unit: 'm/s' } } },
    attribution: '합성 공공 관측 (모의 어댑터)', redistribution_allowed: false, spec_status: 'NEEDS_CONFIRMATION', enabled: true,
    collected_until: '2026-11-05T00:00:00Z', last_success_at: '2026-11-05T00:05:00Z', consecutive_failures: 2, last_error: '응답 시간 초과 (합성)' }] }),
}

/** @type {PostRoutes} */
const POST = {
  // 프록시 재생·원본 내려받기: 단기 URL 대신 합성 시험 패턴 영상 (구간 이동이 되도록 data: URL)
  '/v1/assets/{asset_id}/download-url': ({ params, media }) => ({ status: 200, json: { asset_id: params.asset_id, url: media.video, expires_at: LATER } }),
  '/v1/backup-requests': ({ body }) => {
    const b = /** @type {{ device_id?: string, dry_run?: boolean }} */ (body ?? {})
    return { status: 200, json: { device_id: b.device_id ?? DEV_CAM, local_copy_policy: 'ROTATING', asset_count: 42, total_bytes: 9_876_543_210,
      applied: b.dry_run === false, usage: { quota_bytes: 1_099_511_627_776, used_total_bytes: 268_435_456_000, after_bytes: 278_311_999_210 } } }
  },
  // 이용기관 다운로드 링크 (단기 URL은 합성 주소, 브라우저가 열지 않는다)
  '/v1/distributions': () => {
    const files = [1, 2, 3].map((n) => ({ path: `video/000${n}.mp4`, url: `https://storage.example.org/datasets/${DSV1}/video/000${n}.mp4`,
      byte_size: 244_667_733, checksum_sha256: sha(20 + n) }))
    return { status: 201, json: { distribution_id: id('dlv', 1), grant_id: GRT1, dataset_version_id: DSV1, expires_at: LATER, files,
      manifest: { schema_version: '1.0.0', dataset_id: DST1, dataset_version_id: DSV1, version: '1.0.0', created_at: '2026-11-04T00:00:00Z',
        provider_organization_ids: [ORG_A], item_count: 3, total_bytes: 734_003_200, data_card: DATA_CARD,
        items: files.map((f, i) => ({ asset_id: id('ast', 20 + i), source_asset_id: AST_CANDIDATE, variant: /** @type {const} */ ('DEID'), path: f.path,
          byte_size: f.byte_size, checksum_sha256: f.checksum_sha256, privacy_status: /** @type {const} */ ('ANONYMIZED_VERIFIED'),
          sharing_scope: /** @type {const} */ ('APPROVED_PARTNERS'), legal_basis_ref: '합성 법률 검토 2026-03' })) } } }
  },
  '/v1/retention/dry-run': () => ({ status: 200, json: { generated_at: at('10:00:00'), total_count: 14, total_bytes: 3_221_225_472, excluded_legal_hold: 1,
    targets: [{ target: 'PROXY', count: 12, bytes: 3_200_253_952 }, { target: 'QUARANTINE:UNKNOWN_SESSION', count: 2, bytes: 20_971_520 }],
    warnings: [{ asset_id: AST_CLIP, reason: 'CITED_BY_APPROVED_EVIDENCE' }] } }),
}

// ---------------------------------------------------------------- 경로 맞추기

class NotFound extends Error {}
/** @returns {never} */
function notFound() {
  throw new NotFound()
}

/** @param {string[]} templates */
const compile = (templates) => templates
  .map((template) => ({ template, names: [...template.matchAll(/\{(\w+)\}/g)].map((m) => m[1]),
    re: new RegExp(`^${template.replace(/\{\w+\}/g, '([^/]+)')}$`) }))
  .sort((a, b) => a.names.length - b.names.length) // 고정 경로를 변수 경로보다 먼저 (/v1/inspections/templates 같은 경우)
const ROUTES = { GET: compile(Object.keys(GET)), POST: compile(Object.keys(POST)) }

/**
 * 요청 하나에 대한 합성 응답. 흉내 내지 않은 경로면 undefined (검사 실패로 센다)
 * @param {string} method
 * @param {string} pathname
 * @param {Omit<Ctx, 'params'>} base
 * @returns {{ status: number, json: unknown } | undefined}
 */
export function respond(method, pathname, base) {
  if (method !== 'GET' && method !== 'POST') return undefined
  for (const r of ROUTES[method]) {
    const m = r.re.exec(pathname)
    if (!m) continue
    const params = Object.fromEntries(r.names.map((n, i) => [n, decodeURIComponent(m[i + 1])]))
    const ctx = { ...base, params }
    try {
      if (method === 'GET') {
        const fn = /** @type {(ctx: Ctx) => unknown} */ (GET[/** @type {keyof Paths} */ (r.template)])
        return { status: 200, json: fn(ctx) }
      }
      const fn = /** @type {(ctx: Ctx) => { status: number, json: unknown }} */ (POST[/** @type {keyof Paths} */ (r.template)])
      return fn(ctx)
    } catch (e) {
      if (!(e instanceof NotFound)) throw e
      return { status: 404, json: { type: '/problems/not-found', title: '찾을 수 없음', status: 404, code: 'NOT_FOUND', request_id: '01K9A11Y000000000000000000' } }
    }
  }
  return undefined
}
