// 실증회차 타임라인 (09번 요구사항 5.2): 자산 촬영 구간·이벤트·관측값 수집 구간(07번)·환경 정보(12번)를 한 시간축에 놓고
// 회차 기록·보고서(10번)로 이동한다. 계약에 있는 조회 API만 쓴다. 회차 수정(요구사항 5.1)은 계약에 수정 API가 없어 아직 없다.
import { useQueries, useQuery, type UseQueryResult } from '@tanstack/react-query'
import { Alert, Button, Card, Checkbox, Descriptions, Grid, Modal, Result, Select, Space, Spin, Table, Tabs, Tag, Typography } from 'antd'
import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Legend, Timeline, type Group, type Row } from '../components/Timeline'
import type { components, paths } from '../contract/api.gen'
import { api, ApiError, type Me, type Page } from '../lib/api'
import { EVENT_SOURCE_LABEL, eventLabel, kst, kstRange, QUARANTINE_LABEL, SEVERITY_COLOR, STATUS_LABEL } from '../lib/format'
import { explain } from '../lib/problems'
import { SessionEditButton } from './SessionEdit'

// 계약에서 생성한 타입 (09번 작업 16)
type SessionT = components['schemas']['Session']
type AssetT = components['schemas']['asset-metadata.schema']
type EventT = components['schemas']['EventRecord']
type DeviceT = components['schemas']['Device']
type ObsList = components['schemas']['ObservationList']
type ObsAgg = components['schemas']['ObservationAggregateList']
type ObsQuery = paths['/v1/observations']['get']['parameters']['query']
type Overlap = paths['/v1/events/{event_id}/overlapping-assets']['get']['responses'][200]['content']['application/json']
// env_context.observations 항목은 계약에서 object로만 정해져 있다. 12번 Env_Joiner가 채우는 필드 (backend/app/services/external.py)
interface EnvObs {
  metric?: string; value?: number | null; unit?: string | null; observed_at?: string
  station_name?: string; distance_km?: number; time_diff_s?: number; attribution?: string
}
interface Combo { device: string; metric: string; unit: string | null; interval: string; stepMs: number }
interface Coverage extends Combo {
  total: number; filled: number; count: number; missing: number; min: number | null; max: number | null
  segments: { start: number; end: number; data: boolean }[]; loading: boolean; failed: boolean
}

const MIN = 60_000
const HOUR = 60 * MIN
const PAGE = 500 // 계약 Limit 최댓값
const MAX_PAGES = 4 // 자산·이벤트는 2,000건까지 불러온다
const MAX_OBS_DEVICES = 20 // 관측 항목을 찾아볼 장비 수 한도
const MAX_COMBOS = 40 // 구간 집계를 부를 장비·항목 수 한도 (요청 수 제한)
const BUCKET_TARGET = 120 // 관측 집계 칸 수 목표
const BUCKET_MIN = [1, 2, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440]

const ms = (iso: string | null | undefined) => (iso ? Date.parse(iso) : NaN)
const utc = (t: number) => new Date(t).toISOString().replace(/\.\d+Z$/, 'Z')
const fileOf = (a: AssetT) => a.provenance.original_relative_path ?? a.provenance.original_filename ?? a.asset_id
const pctText = (n: number, d: number) => (d ? `${Math.round((n / d) * 100)}%` : '-')
const ENV_WARNING: Record<string, string> = { NO_STATION_IN_RADIUS: '반경 안 관측소 없음', NO_OBSERVATION_IN_WINDOW: '촬영 시각 근처 관측값 없음' }

function query(params: Record<string, string | number | undefined>): string {
  const q = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== '') q.set(k, String(v))
  return q.toString()
}

// 목록 API를 next_cursor로 이어서 부른다 (한도를 넘으면 truncated)
async function collect<T>(path: string, params: Record<string, string>): Promise<{ items: T[]; truncated: boolean }> {
  const items: T[] = []
  let cursor: string | undefined
  for (let i = 0; i < MAX_PAGES; i++) {
    const page = await api<Page<T>>(`${path}?${query({ ...params, limit: PAGE, cursor })}`)
    items.push(...page.items)
    if (!page.next_cursor) return { items, truncated: false }
    cursor = page.next_cursor
  }
  return { items, truncated: true }
}

// 시간축 범위: 회차 기간 + 회차 자산·이벤트 시각. 종료가 없으면 지금까지 (진행 중)
function domainOf(s: SessionT, assets: AssetT[], events: EventT[], now: number): [number, number] {
  const ts = [ms(s.starts_at), s.ends_at ? ms(s.ends_at) : Math.max(now, ms(s.starts_at))]
  for (const a of assets) ts.push(ms(a.capture_start_at), ms(a.capture_end_at))
  for (const e of events) ts.push(ms(e.started_at), ms(e.ended_at))
  const ok = ts.filter(Number.isFinite)
  if (!ok.length) return [now - HOUR, now] // 회차 시작 시각을 읽지 못한 경우
  const from = ok.reduce((x, y) => Math.min(x, y))
  const to = ok.reduce((x, y) => Math.max(x, y))
  return [from, Math.max(to, from + HOUR)]
}

// 집계 간격: 칸 수가 목표 이하이고 장비의 기대 수집 주기보다 짧지 않게 (계약 interval 형식 ^PT[0-9]+[MH]$)
function bucketFor(spanMs: number, expectedS?: number | null): { interval: string; stepMs: number } {
  const need = Math.max(spanMs / BUCKET_TARGET, (expectedS ?? 0) * 1000)
  const m = BUCKET_MIN.find((b) => b * MIN >= need) ?? Math.ceil(need / HOUR) * 60
  return { interval: m % 60 === 0 ? `PT${m / 60}H` : `PT${m}M`, stepMs: m * MIN }
}

// 집계 칸(bucket_start는 from 기준 date_bin)을 이어 붙여 '관측값 있음'·'결측만' 구간으로 만든다
function coverageOf(c: Combo, q: UseQueryResult<ObsAgg> | undefined, from: number, to: number): Coverage {
  const items = [...(q?.data?.items ?? [])].sort((a, b) => ms(a.bucket_start) - ms(b.bucket_start))
  const segments: Coverage['segments'] = []
  let filled = 0, count = 0, missing = 0
  let min: number | null = null, max: number | null = null
  for (const b of items) {
    const start = ms(b.bucket_start)
    if (!Number.isFinite(start)) continue
    const end = Math.min(start + c.stepMs, to)
    const data = b.count - b.missing_count > 0
    if (data) filled++
    count += b.count
    missing += b.missing_count
    if (b.min != null) min = min == null ? b.min : Math.min(min, b.min)
    if (b.max != null) max = max == null ? b.max : Math.max(max, b.max)
    const last = segments[segments.length - 1]
    if (last && last.data === data && start - last.end < 1000) last.end = end
    else segments.push({ start, end, data })
  }
  return { ...c, total: Math.max(1, Math.ceil((to - from) / c.stepMs)), filled, count, missing, min, max, segments,
    loading: !!q?.isPending, failed: !!q?.isError }
}

const mergeSamples = (rs: UseQueryResult<ObsList>[]) => ({
  records: rs.flatMap((r) => r.data?.items ?? []), pending: rs.some((r) => r.isPending), error: rs.find((r) => r.error)?.error ?? null,
})

// 키별로 묶고 표시 이름 순으로 (한국어 정렬)
function groupBy<T>(xs: T[], key: (x: T) => string, name: (k: string) => string): [string, T[]][] {
  const m = new Map<string, T[]>()
  for (const x of xs) {
    const k = key(x)
    const list = m.get(k)
    if (list) list.push(x)
    else m.set(k, [x])
  }
  return [...m.entries()].sort(([a], [b]) => name(a).localeCompare(name(b), 'ko'))
}

function failText(e: unknown): string {
  return e instanceof ApiError ? `${explain(e.code).cause}${e.problem.detail ? ` — ${e.problem.detail}` : ''}` : String(e)
}

export function SessionTimeline({ me }: { me: Me }) {
  const { sessionId = '' } = useParams()
  const canSeeDevices = me.roles.some((r) => r.startsWith('ORG_')) || me.roles.includes('PLATFORM_OPERATOR')
  const [derived, setDerived] = useState(false)
  const [overlapFor, setOverlapFor] = useState<EventT | null>(null)
  const narrow = Grid.useBreakpoint().md === false

  const session = useQuery({ queryKey: ['session', sessionId], queryFn: () => api<SessionT>(`/v1/sessions/${sessionId}`) })
  const assets = useQuery({ queryKey: ['assets', 'timeline', sessionId], queryFn: () => collect<AssetT>('/v1/assets', { session_id: sessionId }) })
  const events = useQuery({ queryKey: ['events', 'timeline', sessionId], queryFn: () => collect<EventT>('/v1/events', { session_id: sessionId }) })
  const projects = useQuery({ queryKey: ['projects'], queryFn: () => api<Page<{ project_id: string; name: string }>>('/v1/projects?limit=500') })
  const devices = useQuery({ queryKey: ['devices'], queryFn: () => api<Page<DeviceT>>('/v1/devices?limit=500'), enabled: canSeeDevices })

  const deviceById = useMemo(() => new Map((devices.data?.items ?? []).map((d) => [d.device_id, d])), [devices.data])
  const devName = (id: string) => deviceById.get(id)?.name ?? id
  const allAssets = useMemo(() => assets.data?.items ?? [], [assets.data])
  const allEvents = useMemo(() => [...(events.data?.items ?? [])].sort((a, b) => ms(a.started_at) - ms(b.started_at)), [events.data])
  const settled = !!session.data && !assets.isPending && !events.isPending
  const range = useMemo(() => (session.data && settled ? domainOf(session.data, allAssets, allEvents, Date.now()) : null),
    [session.data, settled, allAssets, allEvents])
  const obsFrom = range ? utc(range[0]) : ''
  const obsTo = range ? utc(range[1]) : ''

  // 관측값(07번): 조회 API는 회차로 거르지 못하므로 시간 창 + 장비·항목별 집계로 수집 구간을 본다.
  // 항목 찾기 = 창 전체 표본 + 후보 장비(회차 장비·센서 자산·이벤트 장비·같은 사업 센서)별 표본 + 센서 자산의 sensor_ext.metrics
  const obsSample = useQuery({
    queryKey: ['observations', 'sample', obsFrom, obsTo],
    queryFn: () => api<ObsList>(`/v1/observations?${query({ from: obsFrom, to: obsTo, limit: PAGE } satisfies ObsQuery)}`),
    enabled: !!range,
  })
  const candidates = useMemo(() => {
    if (!session.data || !range) return { ids: [] as string[], more: false }
    const ids = new Set<string>(session.data.device_ids ?? [])
    for (const a of allAssets) if (a.data_type === 'SENSOR') ids.add(a.source_system_id)
    for (const e of allEvents) if (e.source_system_id) ids.add(e.source_system_id)
    for (const d of devices.data?.items ?? [])
      if (d.kind === 'SOURCE' && d.device_type === 'SENSOR' && d.project_id === session.data.project_id) ids.add(d.device_id)
    for (const o of obsSample.data?.items ?? []) ids.add(o.source_system_id)
    const all = [...ids]
    return { ids: all.slice(0, MAX_OBS_DEVICES), more: all.length > MAX_OBS_DEVICES }
  }, [session.data, range, allAssets, allEvents, devices.data, obsSample.data])
  const samples = useQueries({
    queries: candidates.ids.map((d) => ({
      queryKey: ['observations', 'sample', d, obsFrom, obsTo],
      queryFn: () => api<ObsList>(`/v1/observations?${query({ source_system_id: d, from: obsFrom, to: obsTo, limit: 200 } satisfies ObsQuery)}`),
      enabled: !!range,
    })),
    combine: mergeSamples,
  })
  const combos = useMemo(() => {
    if (!range) return { list: [] as Combo[], more: false }
    const found = new Map<string, Combo>()
    const add = (device: string, metric: string, unit?: string | null) => {
      const cur = found.get(`${device}|${metric}`)
      if (cur) { if (!cur.unit && unit) cur.unit = unit; return }
      found.set(`${device}|${metric}`, { device, metric, unit: unit ?? null, ...bucketFor(range[1] - range[0], deviceById.get(device)?.expected_interval_s) })
    }
    for (const o of [...(obsSample.data?.items ?? []), ...samples.records]) add(o.source_system_id, o.metric, o.unit)
    for (const a of allAssets) if (a.data_type === 'SENSOR') for (const m of a.sensor_ext?.metrics ?? []) add(a.source_system_id, m)
    const sortKey = (c: Combo) => `${deviceById.get(c.device)?.name ?? c.device} ${c.metric}`
    const all = [...found.values()].sort((a, b) => sortKey(a).localeCompare(sortKey(b), 'ko'))
    return { list: all.slice(0, MAX_COMBOS), more: all.length > MAX_COMBOS }
  }, [range, obsSample.data, samples.records, allAssets, deviceById])
  const aggs = useQueries({
    queries: combos.list.map((c) => ({
      queryKey: ['observations', 'agg', c.device, c.metric, obsFrom, obsTo, c.interval],
      queryFn: () => api<ObsAgg>(`/v1/observations?${query({ source_system_id: c.device, metric: c.metric, from: obsFrom, to: obsTo, interval: c.interval } satisfies ObsQuery)}`),
      enabled: !!range,
    })),
  })
  const coverage = range ? combos.list.map((c, i) => coverageOf(c, aggs[i], range[0], range[1])) : []
  const obsLoading = !!range && (obsSample.isPending || samples.pending || coverage.some((c) => c.loading))
  const obsError = obsSample.error ?? samples.error

  // 환경 정보(12번): 자산 env_context에 붙은 공공 관측값. 같은 관측(항목·관측소·시각)은 한 번만
  const env = useMemo(() => {
    const seen = new Map<string, { o: EnvObs; t: number; asset: AssetT }>()
    const warnings = new Map<string, number>()
    for (const a of allAssets) {
      for (const raw of a.env_context?.observations ?? []) {
        const o = raw as EnvObs
        const t = ms(o.observed_at ?? a.capture_start_at)
        const k = `${o.metric}|${o.station_name ?? ''}|${t}`
        if (o.metric && Number.isFinite(t) && !seen.has(k)) seen.set(k, { o, t, asset: a })
      }
      for (const w of a.env_context?.warnings ?? []) warnings.set(w.code, (warnings.get(w.code) ?? 0) + 1)
    }
    return { points: [...seen.values()].sort((x, y) => x.t - y.t), warnings: [...warnings.entries()] }
  }, [allAssets])

  if (session.error)
    return <Result status={session.error instanceof ApiError && session.error.problem.status === 404 ? '404' : 'error'} title="회차를 불러오지 못했습니다" subTitle={failText(session.error)}
      extra={<Link to="/sessions">회차 목록으로</Link>} />
  const s = session.data
  if (!s) return <Card loading />

  const visible = allAssets.filter((a) => derived || a.variant === 'ORIGINAL')
  const known = visible.filter((a) => a.capture_start_at)
  const unknown = visible.filter((a) => !a.capture_start_at)

  const assetRows: Row[] = groupBy(known, (a) => a.source_system_id, devName).map(([dev, xs]) => ({
    key: dev, title: devName(dev), sub: `${xs.length}건`,
    marks: xs.map((a) => {
      const verified = a.timestamp_quality.verified
      return {
        key: a.asset_id, start: ms(a.capture_start_at), end: a.capture_end_at ? ms(a.capture_end_at) : undefined, href: `/assets/${a.asset_id}`,
        tone: verified ? 'asset' : 'assetUnverified',
        label: `${fileOf(a)} (${a.data_type}·${a.variant}), 촬영 ${kstRange(a.capture_start_at, a.capture_end_at)}`
          + `${verified ? '' : `, 시각 미검증(${a.timestamp_quality.source})`} — 자산 상세 보기`,
      }
    }),
  }))
  const eventRows: Row[] = groupBy(allEvents, (e) => e.source_system_id ?? '', devName).map(([dev, xs]) => ({
    key: dev || 'none', title: dev ? devName(dev) : '장비 없음 (수동 입력 등)', sub: `${xs.length}건`,
    marks: xs.map((e) => {
      const critical = e.severity === 'CRITICAL'
      return {
        key: e.event_id, start: ms(e.started_at), end: e.ended_at ? ms(e.ended_at) : undefined, onClick: () => setOverlapFor(e),
        tone: critical ? (e.human_verified ? 'eventCritical' : 'eventCriticalUnverified') : e.human_verified ? 'event' : 'eventUnverified',
        label: `${eventLabel(e.event_code)}, ${EVENT_SOURCE_LABEL[e.source] ?? e.source}, ${e.human_verified ? '사람 확인' : '미확인'}`
          + `${e.severity ? `, ${e.severity}` : ''}, ${kstRange(e.started_at, e.ended_at)} — 겹치는 자산 보기`,
      }
    }),
  }))
  const obsRows: Row[] = coverage.map((c) => ({
    key: `${c.device}|${c.metric}`, flat: true, title: <>{devName(c.device)} · <code>{c.metric}</code></>,
    sub: c.loading ? '불러오는 중' : c.failed ? '불러오지 못함' : `${c.count}건 · 채움 ${pctText(c.filled, c.total)}${c.missing ? ` · 결측 ${c.missing}` : ''}`,
    marks: c.segments.map((g) => ({
      key: `${g.start}`, start: g.start, end: g.end, tone: g.data ? 'obs' : 'obsMissing',
      label: `${devName(c.device)} ${c.metric} ${g.data ? '관측값 있음' : '결측만 있음'} ${kstRange(utc(g.start), utc(g.end))} (집계 ${c.interval})`,
    })),
  }))
  const envRows: Row[] = groupBy(env.points, (p) => p.o.metric ?? '', (m) => m).map(([metric, xs]) => ({
    key: metric, title: <code>{metric}</code>, sub: `${xs.length}건`,
    marks: xs.map((p) => ({
      key: `${p.o.station_name}|${p.t}`, start: p.t, tone: 'env',
      label: `${metric} ${p.o.value ?? '-'} ${p.o.unit ?? ''}, ${p.o.station_name ?? '관측소 미상'}${p.o.distance_km != null ? ` ${p.o.distance_km}km` : ''}, 관측 ${kst(utc(p.t))} (자산 ${fileOf(p.asset)})`,
    })),
  }))

  const groups: Group[] = [
    { key: 'assets', title: `자산 촬영 구간 (${known.length}건)`, rows: assetRows, empty: '촬영시각이 있는 자산이 없습니다' },
    { key: 'events', title: `이벤트 (${allEvents.length}건)`, rows: eventRows, empty: '이 회차의 이벤트가 없습니다' },
    { key: 'obs', title: `관측값 수집 구간 (장비·항목 ${coverage.length}개)`, rows: obsRows,
      empty: obsLoading ? '관측값을 찾는 중입니다' : obsError ? '관측 구간을 그리지 못했습니다' : '이 기간의 관측값이 없습니다' },
    { key: 'env', title: `환경 정보 (공공 관측 ${env.points.length}건)`, rows: envRows, empty: '자산에 붙은 환경 정보가 없습니다' },
  ]
  const project = projects.data?.items.find((p) => p.project_id === s.project_id)
  // 회차 기록·보고서(10번)로 이동 (요구사항 5.2)
  const canEdit = me.roles.some((r) => r === 'ORG_ADMIN' || r === 'ORG_MEMBER')
  const links = (
    <Space wrap size="middle">
      {canEdit && <SessionEditButton session={s} devices={devices.data?.items ?? []} />}
      <Link to={`/sessions/${s.session_id}/records`}>회차 기록</Link>
      <Link to="/reports">보고서</Link>
      <Link to="/sessions">회차 목록</Link>
    </Space>
  )

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card title={<span>실증회차 <code>{s.label}</code> · {s.title}</span>} styles={{ title: { whiteSpace: 'normal' } }}
        extra={!narrow && links}>
        {narrow && <div style={{ marginBottom: 12 }}>{links}</div>}
        <Descriptions size="small" bordered column={{ xs: 1, md: 2 }}>
          <Descriptions.Item label="실증사업">{project?.name ?? s.project_id}</Descriptions.Item>
          <Descriptions.Item label="기간">{s.ends_at ? kstRange(s.starts_at, s.ends_at) : `${kst(s.starts_at)} ~ 종료 미정 (진행 중)`}</Descriptions.Item>
          <Descriptions.Item label="장소">{s.site ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="회차 장비">{(s.device_ids ?? []).length ? (s.device_ids ?? []).map(devName).join(', ') : '지정 안 됨'}</Descriptions.Item>
          <Descriptions.Item label="메모">{s.note ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="자료">
            자산 {allAssets.length}건 (원본 {allAssets.filter((a) => a.variant === 'ORIGINAL').length}건) · 이벤트 {allEvents.length}건
          </Descriptions.Item>
        </Descriptions>
      </Card>

      <Card title="회차 타임라인">
        <Space direction="vertical" style={{ width: '100%' }}>
          <Checkbox checked={derived} onChange={(e) => setDerived(e.target.checked)}>파생 자산(프록시·클립 등)도 보기</Checkbox>
          {(assets.error || events.error) && <Alert type="error" showIcon message="자산·이벤트를 불러오지 못했습니다" description={failText(assets.error ?? events.error)} />}
          {(assets.data?.truncated || events.data?.truncated) && (
            <Alert type="warning" showIcon message={`자산·이벤트가 많아 각각 처음 ${(PAGE * MAX_PAGES).toLocaleString('ko-KR')}건만 표시합니다. 자산 탐색 화면에서 기간으로 나눠 보세요.`} />
          )}
          <Legend items={[
            { tone: 'band', text: '회차 기간' },
            { tone: 'asset', text: '자산 촬영 구간' }, { tone: 'assetUnverified', text: '촬영시각 미검증' },
            { tone: 'event', text: '이벤트(사람 확인)' }, { tone: 'eventUnverified', text: '이벤트(미확인)' }, { tone: 'eventCritical', text: '심각(CRITICAL)' },
            { tone: 'obs', text: '관측값 있음' }, { tone: 'obsMissing', text: '결측만' }, { tone: 'env', text: '공공 관측', instant: true },
          ]} />
          {range ? (
            <>
              <Typography.Text type="secondary">
                시간축 {kstRange(utc(range[0]), utc(range[1]))}. 자산 표시는 상세 화면으로, 이벤트 표시는 겹치는 자산 목록으로 이어집니다 (Tab으로 이동).
              </Typography.Text>
              <Timeline label="회차 타임라인" from={range[0] - (range[1] - range[0]) * 0.02} to={range[1] + (range[1] - range[0]) * 0.02}
                band={[ms(s.starts_at), s.ends_at ? ms(s.ends_at) : range[1]]}
                groups={groups.map((g) => g.key !== 'obs' ? g : { ...g, note: (
                  <>
                    {obsLoading && <Spin size="small" />}
                    {obsError && <Alert type="warning" showIcon message={`관측값을 불러오지 못했습니다: ${failText(obsError)}`} />}
                    {candidates.more && <Typography.Paragraph type="secondary" style={{ margin: 0 }}>장비가 많아 {MAX_OBS_DEVICES}대까지만 관측 항목을 찾았습니다.</Typography.Paragraph>}
                    {combos.more && <Typography.Paragraph type="secondary" style={{ margin: 0 }}>장비·항목이 많아 {MAX_COMBOS}개까지만 집계했습니다.</Typography.Paragraph>}
                  </>
                ) })} />
            </>
          ) : <Spin />}
          {unknown.length > 0 && (
            <Alert type="info" showIcon message={`촬영시각 미상 자산 ${unknown.length}건은 시간축에 놓지 않았습니다. 아래 '촬영시각 미상' 탭에서 볼 수 있습니다.`} />
          )}
          {env.warnings.length > 0 && (
            <Typography.Text type="secondary">
              환경 정보를 붙이지 못한 자산: {env.warnings.map(([code, n]) => `${ENV_WARNING[code] ?? code} ${n}건`).join(', ')}
            </Typography.Text>
          )}
        </Space>
      </Card>

      <Card>
        <Tabs items={[
          { key: 'events', label: `이벤트 (${allEvents.length})`, children: (
            <Table rowKey="event_id" size="small" scroll={{ x: 'max-content' }} dataSource={allEvents} loading={events.isLoading} pagination={{ pageSize: 20 }} locale={{ emptyText: '이벤트가 없습니다' }}
              columns={[
                { title: '시작', dataIndex: 'started_at', render: kst },
                { title: '끝', dataIndex: 'ended_at', render: kst },
                { title: '분류', dataIndex: 'event_code', render: (c: string) => <span>{eventLabel(c)} <Typography.Text type="secondary" code>{c}</Typography.Text></span> },
                { title: '출처', key: 'src', render: (_, e) => `${EVENT_SOURCE_LABEL[e.source] ?? e.source}${e.source === 'MODEL' && e.confidence != null ? ` (${Math.round(e.confidence * 100)}%)` : ''}` },
                { title: '장비', key: 'dev', render: (_, e) => (e.source_system_id ? devName(e.source_system_id) : '-') },
                { title: '확인', key: 'hv', render: (_, e) => <Tag color={e.human_verified ? 'green' : 'default'}>{e.human_verified ? '사람 확인' : '미확인'}</Tag> },
                { title: '심각도', dataIndex: 'severity', render: (v?: string) => (v ? <Tag color={SEVERITY_COLOR[v]}>{v}</Tag> : '-') },
                { title: '연결 자산', key: 'ln', render: (_, e) => (e.linked_asset_ids ?? []).length },
                { title: '', key: 'ov', render: (_, e) => <Button size="small" type="link" onClick={() => setOverlapFor(e)}>겹치는 자산</Button> },
              ]} />
          ) },
          { key: 'unknown', label: `촬영시각 미상 (${unknown.length})`, children: (
            <Table rowKey="asset_id" size="small" scroll={{ x: 'max-content' }} dataSource={unknown} pagination={{ pageSize: 20 }} locale={{ emptyText: '촬영시각 미상 자산이 없습니다' }}
              columns={[
                { title: '파일', key: 'f', render: (_, a) => <Link to={`/assets/${a.asset_id}`}>{fileOf(a)}</Link> },
                { title: '유형·변형', key: 't', render: (_, a) => `${a.data_type} · ${a.variant}` },
                { title: '장비', key: 'd', render: (_, a) => devName(a.source_system_id) },
                { title: '촬영시각', key: 'c', render: () => <Tag>촬영시각 미상</Tag> },
                { title: '받은 시각', dataIndex: 'ingested_at', render: kst },
                { title: '상태', key: 's', render: (_, a) => <>{STATUS_LABEL[a.ingest_status] ?? a.ingest_status}{a.quarantine_reason && ` (${QUARANTINE_LABEL[a.quarantine_reason] ?? a.quarantine_reason})`}</> },
              ]} />
          ) },
          { key: 'obs', label: `관측값 (${coverage.length})`, children: (
            <>
              <Table rowKey={(c) => `${c.device}|${c.metric}`} size="small" scroll={{ x: 'max-content' }} dataSource={coverage} loading={obsLoading && !coverage.length} pagination={false}
                locale={{ emptyText: '이 기간의 관측값이 없습니다' }}
                columns={[
                  { title: '장비', key: 'd', render: (_, c) => devName(c.device) },
                  { title: '항목', dataIndex: 'metric', render: (m: string) => <code>{m}</code> },
                  { title: '단위', dataIndex: 'unit', render: (u: string | null) => u ?? '-' },
                  { title: '집계 간격', dataIndex: 'interval' },
                  { title: '건수(결측 포함)', dataIndex: 'count' },
                  { title: '결측', dataIndex: 'missing' },
                  { title: '채움', key: 'f', render: (_, c) => `${pctText(c.filled, c.total)} (${c.filled}/${c.total}칸)` },
                  { title: '최소~최대', key: 'mm', render: (_, c) => (c.min == null ? '-' : `${+c.min.toFixed(3)} ~ ${+(c.max ?? c.min).toFixed(3)}`) },
                ]} />
              <Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>
                관측값 조회 API는 회차로 거르지 않으므로 시간축 기간의 관측을 장비·항목별 구간 집계(GET /v1/observations, interval)로 봅니다.
                항목은 회차 장비·센서 자산·같은 사업 센서의 표본 조회로 찾으므로, 기간 앞부분 표본에 없던 항목은 빠질 수 있습니다.
              </Typography.Paragraph>
            </>
          ) },
          { key: 'env', label: `환경 정보 (${env.points.length})`, children: (
            <Table rowKey={(p) => `${p.o.metric}|${p.o.station_name}|${p.t}`} size="small" scroll={{ x: 'max-content' }} dataSource={env.points} pagination={{ pageSize: 20 }}
              locale={{ emptyText: '자산에 붙은 환경 정보가 없습니다' }}
              columns={[
                { title: '관측 시각', key: 't', render: (_, p) => kst(utc(p.t)) },
                { title: '항목', key: 'm', render: (_, p) => <code>{p.o.metric}</code> },
                { title: '값', key: 'v', render: (_, p) => `${p.o.value ?? '-'} ${p.o.unit ?? ''}` },
                { title: '관측소', key: 's', render: (_, p) => `${p.o.station_name ?? '-'}${p.o.distance_km != null ? ` (${p.o.distance_km} km)` : ''}` },
                { title: '자산', key: 'a', render: (_, p) => <Link to={`/assets/${p.asset.asset_id}`}>{fileOf(p.asset)}</Link> },
                { title: '출처', key: 'att', render: (_, p) => p.o.attribution ?? '-' },
              ]} />
          ) },
        ]} />
      </Card>

      {overlapFor && <OverlapModal event={overlapFor} devName={devName} onClose={() => setOverlapFor(null)} />}
    </Space>
  )
}

// 이벤트 구간과 촬영 구간이 겹치는 같은 회차 자산 (07번 요구사항 7.6, GET /v1/events/{id}/overlapping-assets)
function OverlapModal({ event, devName, onClose }: { event: EventT; devName: (id: string) => string; onClose: () => void }) {
  const [margin, setMargin] = useState(0)
  const q = useQuery({
    queryKey: ['events', event.event_id, 'overlapping-assets', margin],
    queryFn: () => api<Overlap>(`/v1/events/${event.event_id}/overlapping-assets?margin_s=${margin}`),
  })
  return (
    <Modal open title={`겹치는 자산 — ${eventLabel(event.event_code)}`} onCancel={onClose} footer={null} width={960}>
      <Space direction="vertical" style={{ width: '100%' }}>
        <Typography.Text>
          이벤트 {kstRange(event.started_at, event.ended_at)} · {EVENT_SOURCE_LABEL[event.source] ?? event.source} · {event.human_verified ? '사람 확인' : '미확인'}
        </Typography.Text>
        <Space>
          <label htmlFor="overlap-margin">앞뒤 여유</label>
          <Select id="overlap-margin" virtual={false} style={{ width: 120 }} value={margin} onChange={setMargin}
            options={[0, 60, 300, 600, 1800].map((v) => ({ value: v, label: v ? `${v / 60}분` : '없음' }))} />
        </Space>
        {q.error && <Alert type="error" showIcon message={failText(q.error)} />}
        <Table rowKey={(x) => x.asset.asset_id} size="small" scroll={{ x: 'max-content' }} loading={q.isLoading} dataSource={q.data?.items ?? []} pagination={{ pageSize: 10 }}
          locale={{ emptyText: '겹치는 자산이 없습니다' }}
          columns={[
            { title: '파일', key: 'f', render: (_, x) => <Link to={`/assets/${x.asset.asset_id}`}>{fileOf(x.asset)}</Link> },
            { title: '장비', key: 'd', render: (_, x) => devName(x.asset.source_system_id) },
            { title: '촬영', key: 'c', render: (_, x) => kstRange(x.asset.capture_start_at, x.asset.capture_end_at) },
            { title: '겹침', key: 'o', render: (_, x) => `${Math.round(x.overlap_s)}초` },
            { title: '연결', key: 'l', render: (_, x) => (x.linked ? <Tag color="green">연결됨</Tag> : '-') },
          ]} />
        {!!q.data?.unknown_capture_count && (
          <Typography.Text type="secondary">촬영시각 미상 자산 {q.data.unknown_capture_count}건은 비교하지 않았습니다.</Typography.Text>
        )}
      </Space>
    </Modal>
  )
}
