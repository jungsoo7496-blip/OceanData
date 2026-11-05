// 자산 탐색·상세·영상 뷰어·구간 클립 (09번 요구사항 6, 7).
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, App, Button, Card, Descriptions, Input, InputNumber, Select, Space, Table, Tag, Typography } from 'antd'
import { useRef, useState, type RefObject } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import type { components } from '../contract/api.gen'
import { api, ApiError, post, type Asset, type Device, type Me, type Page, type Session } from '../lib/api'
import { bytes, kst, QUARANTINE_LABEL, SCOPE_LABEL, STATUS_LABEL } from '../lib/format'
import { explain } from '../lib/problems'
import { SceneSearch } from './Review'
import { ShareCard } from './Sharing'

// 촬영시각 미상은 업로드 시각과 구분해 표시한다 (요구사항 6.3)
export function CaptureTime({ a }: { a: Asset }) {
  if (!a.capture_start_at) return <Tag>촬영시각 미상</Tag>
  return (
    <span>
      {kst(a.capture_start_at)} {!a.timestamp_quality.verified && <Tag color="default">{a.timestamp_quality.source} 미검증</Tag>}
    </span>
  )
}

const ROLE_LABEL: Record<string, string> = { CORE: '핵심(CORE)', BYPRODUCT: '부산물(BYPRODUCT)' }

export function Assets({ me }: { me: Me }) {
  const [params, setParams] = useSearchParams()
  const filters = Object.fromEntries(params.entries())
  const qs = new URLSearchParams({ limit: '200', ...filters }).toString()
  const assets = useQuery({ queryKey: ['assets', qs], queryFn: () => api<Page<Asset>>(`/v1/assets?${qs}`) })
  const sessions = useQuery({ queryKey: ['sessions'], queryFn: () => api<Page<Session>>('/v1/sessions?limit=500') })
  const devices = useQuery({ queryKey: ['devices'], queryFn: () => api<Page<Device>>('/v1/devices?limit=500'),
    enabled: me.roles.some((r) => r.startsWith('ORG_')) })
  const set = (k: string, v: string | undefined) => {
    const n = new URLSearchParams(params)
    if (v) n.set(k, v)
    else n.delete(k)
    setParams(n)
  }
  const sel = (k: string, placeholder: string, options: { value: string; label: string }[]) => (
    <Select virtual={false} allowClear style={{ minWidth: 160 }} placeholder={placeholder} aria-label={placeholder} value={filters[k]}
      options={options} onChange={(v: string | undefined) => set(k, v)} />
  )
  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
    <SceneSearch />
    <Card title="자산">
      <Space wrap style={{ marginBottom: 16 }}>
        {sel('session_id', '회차', (sessions.data?.items ?? []).map((s) => ({ value: s.session_id, label: s.label })))}
        {sel('source_system_id', '장비', (devices.data?.items ?? []).filter((d) => d.kind === 'SOURCE').map((d) => ({ value: d.device_id, label: d.name })))}
        {sel('data_type', '데이터 유형', ['VIDEO', 'IMAGE', 'SENSOR', 'DOCUMENT', 'LOG'].map((v) => ({ value: v, label: v })))}
        {sel('variant', '변형', ['ORIGINAL', 'CLIP', 'PROXY', 'THUMBNAIL'].map((v) => ({ value: v, label: v })))}
        {sel('quality_status', '품질', ['PASSED', 'WARNING', 'FAILED', 'PENDING'].map((v) => ({ value: v, label: v })))}
        {sel('ingest_status', '상태', Object.entries(STATUS_LABEL).map(([value, label]) => ({ value, label })))}
        {sel('sharing_scope', '공유 등급', Object.entries(SCOPE_LABEL).map(([value, label]) => ({ value, label })))}
        {sel('env_metric', '환경 항목', ['significant_wave_height', 'wind_speed', 'sea_water_temperature', 'visibility'].map((v) => ({ value: v, label: v })))}
        {filters.env_metric && <Input style={{ width: 120 }} placeholder="최솟값" aria-label="환경 최솟값" defaultValue={filters.env_min} onPressEnter={(e) => set('env_min', (e.target as HTMLInputElement).value || undefined)} />}
      </Space>
      <Table rowKey="asset_id" loading={assets.isLoading} dataSource={assets.data?.items ?? []} pagination={{ pageSize: 50 }}
        columns={[
          { title: '파일', key: 'f', render: (_, a) => <Link to={`/assets/${a.asset_id}`}>{a.provenance.original_relative_path ?? a.provenance.original_filename ?? a.asset_id}</Link> },
          { title: '유형·변형', key: 't', render: (_, a) => `${a.data_type} · ${a.variant}` },
          { title: '촬영시각', key: 'c', render: (_, a) => <CaptureTime a={a} /> },
          { title: '받은 시각', dataIndex: 'ingested_at', render: kst },
          { title: '크기', dataIndex: 'byte_size', render: bytes },
          { title: '상태', key: 's', render: (_, a) => (
            <Space size={4}>
              <Tag color={a.ingest_status === 'QUARANTINED' ? 'red' : a.ingest_status === 'READY_PRIVATE' ? 'green' : 'blue'}>{STATUS_LABEL[a.ingest_status] ?? a.ingest_status}</Tag>
              {a.quarantine_reason && <Tag color="red">{QUARANTINE_LABEL[a.quarantine_reason]}</Tag>}
            </Space>
          ) },
          { title: '등급', key: 'l', render: (_, a) => <Tag>{SCOPE_LABEL[a.sharing_scope] ?? a.sharing_scope}</Tag> },
          { title: '역할', key: 'r', render: (_, a) => ROLE_LABEL[a.data_role] ?? a.data_role },
        ]} />
    </Card>
    </Space>
  )
}

type ClipJob = components['schemas']['ClipJob']
const CLIP_STATUS: Record<ClipJob['status'], { label: string; color: string }> = {
  QUEUED: { label: '대기', color: 'default' }, RUNNING: { label: '만드는 중', color: 'processing' },
  DONE: { label: '완료', color: 'green' }, FAILED: { label: '실패', color: 'red' },
}
const CLIP_ERROR: Record<string, string> = {
  SOURCE_UNAVAILABLE: '원 영상이 격리·삭제되어 만들지 못했습니다',
  CLIP_ENCODE_FAILED: '영상을 자르지 못했습니다 (파일 손상 가능 — 운영기관에 문의)',
  QUOTA_EXCEEDED: '기관 저장 쿼터가 모자라 저장하지 못했습니다 (자료를 정리하거나 쿼터 증설을 문의한 뒤 다시 요청)',
}
const CLIP_SOURCE_STATUS = ['REGISTERED', 'PROXIED', 'READY_PRIVATE']
const mmss = (s: number) => `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, '0')}`
const ms = (s: number) => Math.round(s * 1000) / 1000

// 구간 클립 내보내기 (요구사항 7.2): 재생 위치나 숫자로 구간을 정하면 작업 큐가 원 영상에서 잘라 파생 자산(CLIP)으로 연결한다
function ClipCard({ asset, videoRef, playing }: { asset: Asset; videoRef: RefObject<HTMLVideoElement>; playing: boolean }) {
  const { message } = App.useApp()
  const qc = useQueryClient()
  const duration = asset.video_ext?.duration_s
  const [start, setStart] = useState<number | null>(null)
  const [end, setEnd] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const clips = useQuery({
    queryKey: ['clips', asset.asset_id],
    queryFn: () => api<{ items: ClipJob[] }>(`/v1/assets/${asset.asset_id}/clips`),
    refetchInterval: (q) => (q.state.data?.items.some((c) => c.status === 'QUEUED' || c.status === 'RUNNING') ? 5000 : false),
  })
  const create = useMutation({
    mutationFn: () => post<ClipJob>(`/v1/assets/${asset.asset_id}/clips`, { start_s: start, end_s: end }),
    onSuccess: () => {
      setError(null)
      void message.success('클립 작업을 등록했습니다. 완료되면 아래 목록과 계보에 나타납니다')
      void qc.invalidateQueries({ queryKey: ['clips', asset.asset_id] })
      void qc.invalidateQueries({ queryKey: ['lineage', asset.asset_id] })
    },
    onError: (e) => setError(e instanceof ApiError
      ? (e.problem.errors?.map((x) => x.message).join(' / ') || e.problem.detail || explain(e.code).cause) : String(e)),
  })
  const here = (set: (v: number) => void) => {
    const t = videoRef.current?.currentTime
    if (t !== undefined) set(ms(t))
  }
  const ready = CLIP_SOURCE_STATUS.includes(asset.ingest_status) && !!duration
  const invalid = start === null || end === null || end <= start || (!!duration && end > duration)
  return (
    <Card title="구간 클립 내보내기">
      {!ready ? (
        <Alert type="info" showIcon message={duration ? '이 상태의 영상은 자를 수 없습니다' : '영상 길이를 아직 모릅니다. 메타 추출이 끝나면 자를 수 있습니다'} />
      ) : (
        <Space direction="vertical" style={{ width: '100%' }}>
          {error && <Alert type="error" showIcon message={error} closable onClose={() => setError(null)} />}
          <Space wrap>
            <InputNumber aria-label="시작 (초)" addonBefore="시작" addonAfter="초" min={0} max={duration} step={0.1} value={start} onChange={(v) => setStart(v)} />
            <Button onClick={() => here(setStart)} disabled={!playing}>현재 위치를 시작으로</Button>
            <InputNumber aria-label="끝 (초)" addonBefore="끝" addonAfter="초" min={0} max={duration} step={0.1} value={end} onChange={(v) => setEnd(v)} />
            <Button onClick={() => here(setEnd)} disabled={!playing}>현재 위치를 끝으로</Button>
            <Button type="primary" disabled={invalid} loading={create.isPending} onClick={() => create.mutate()}>클립 만들기</Button>
          </Space>
          <Typography.Text type="secondary">
            영상 길이 {mmss(duration)} · 원 영상에서 프레임 단위로 다시 인코딩해 자르며, 결과는 이 영상의 파생 자산(CLIP, 비공개)이 됩니다.
            {!playing && ' 위에서 프록시를 재생하면 재생 위치로 구간을 정할 수 있습니다.'}
          </Typography.Text>
        </Space>
      )}
      <Table size="small" style={{ marginTop: 16 }} rowKey="job_id" loading={clips.isLoading} pagination={false}
        locale={{ emptyText: '요청한 클립이 없습니다' }} dataSource={clips.data?.items ?? []}
        columns={[
          { title: '구간', key: 'r', render: (_, c) => `${mmss(c.start_s)} ~ ${mmss(c.end_s)} (${ms(c.end_s - c.start_s)}초)` },
          { title: '요청', dataIndex: 'requested_at', render: kst },
          { title: '상태', key: 's', render: (_, c) => <Tag color={CLIP_STATUS[c.status].color}>{CLIP_STATUS[c.status].label}</Tag> },
          { title: '결과', key: 'o', render: (_, c) => c.clip_asset_id ? <Link to={`/assets/${c.clip_asset_id}`}>클립 자산 보기</Link>
            : c.status === 'FAILED' ? (CLIP_ERROR[c.error_code ?? ''] ?? c.error_code ?? '실패') : c.status === 'DONE' ? '삭제됨' : '—' },
        ]} />
    </Card>
  )
}

interface Lineage { descendants: { asset_id: string; relation: string }[]; ancestors: { asset_id: string; relation: string }[] }

export function AssetDetail({ me }: { me: Me }) {
  const { assetId = '' } = useParams()
  const asset = useQuery({ queryKey: ['asset', assetId], queryFn: () => api<Asset>(`/v1/assets/${assetId}`) })
  const lineage = useQuery({ queryKey: ['lineage', assetId], queryFn: () => api<Lineage>(`/v1/assets/${assetId}/lineage`) })
  const [video, setVideo] = useState<string | null>(null)
  const videoRef = useRef<HTMLVideoElement>(null)
  const [error, setError] = useState<string | null>(null)
  const proxy = lineage.data?.descendants.find((d) => d.relation === 'PROXY')
  const isOrg = me.roles.some((r) => r.startsWith('ORG_'))
  // 단기 사전 서명 URL로 프록시 재생 (요구사항 7.1). 발급은 감사 로그에 남는다
  const play = async (id: string) => {
    try {
      setVideo((await post<{ url: string }>(`/v1/assets/${id}/download-url`, {})).url)
    } catch (e) {
      setError(e instanceof ApiError ? explain(e.code).cause : String(e))
    }
  }
  const download = async () => {
    try {
      window.location.href = (await post<{ url: string }>(`/v1/assets/${assetId}/download-url`, {})).url
    } catch (e) {
      setError(e instanceof ApiError ? explain(e.code).cause : String(e))
    }
  }
  const a = asset.data
  const env = (a as (Asset & { env_context?: { observations?: { metric: string; value: number; unit: string; station_name: string; distance_km: number; time_diff_s: number; attribution: string }[]; warnings?: { code: string; message: string }[] } }) | undefined)?.env_context
  if (!a) return <Card loading={asset.isLoading}>{asset.error ? '자산을 찾을 수 없습니다' : null}</Card>
  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      {error && <Alert type="error" showIcon message={error} closable onClose={() => setError(null)} />}
      <Card title={a.provenance.original_filename ?? a.asset_id}
        extra={isOrg && a.has_binary && a.ingest_status !== 'QUARANTINED' && <Button onClick={() => void download()}>원본 내려받기 (감사 기록)</Button>}>
        <Descriptions column={{ xs: 1, md: 2 }} size="small" bordered>
          <Descriptions.Item label="자산 ID">{a.asset_id}</Descriptions.Item>
          <Descriptions.Item label="상태">{STATUS_LABEL[a.ingest_status]} {a.quarantine_reason && `(${QUARANTINE_LABEL[a.quarantine_reason]})`}</Descriptions.Item>
          <Descriptions.Item label="촬영시각"><CaptureTime a={a} /></Descriptions.Item>
          <Descriptions.Item label="받은 시각">{kst(a.ingested_at)}</Descriptions.Item>
          <Descriptions.Item label="공유 등급">{SCOPE_LABEL[a.sharing_scope]}</Descriptions.Item>
          <Descriptions.Item label="역할">{ROLE_LABEL[a.data_role]}</Descriptions.Item>
          <Descriptions.Item label="크기">{bytes(a.byte_size)}</Descriptions.Item>
          <Descriptions.Item label="품질">{a.quality_status} {a.quality_reasons.join(', ')}</Descriptions.Item>
          {a.video_ext && <Descriptions.Item label="영상">{a.video_ext.resolution} · {a.video_ext.fps}fps · {a.video_ext.duration_s}s · {a.video_ext.codec}</Descriptions.Item>}
        </Descriptions>
      </Card>
      {proxy && isOrg && (
        <Card title="프록시 재생">
          {video ? (
            <video ref={videoRef} src={video} controls style={{ maxWidth: '100%' }} aria-label="프록시 영상" />
          ) : (
            <Button type="primary" onClick={() => void play(proxy.asset_id)}>재생</Button>
          )}
        </Card>
      )}
      {isOrg && a.organization_id === me.organization_id && a.data_type === 'VIDEO' && a.has_binary
        && ['ORIGINAL', 'CLIP'].includes(a.variant) && <ClipCard asset={a} videoRef={videoRef} playing={!!video} />}
      {env?.observations?.length ? (
        <Card title="촬영 당시 환경 (가장 가까운 공공 관측)">
          <Table size="small" pagination={false} rowKey="metric" dataSource={env.observations}
            columns={[{ title: '항목', dataIndex: 'metric' }, { title: '값', key: 'v', render: (_, o) => `${o.value} ${o.unit}` },
              { title: '관측소', dataIndex: 'station_name' }, { title: '거리', dataIndex: 'distance_km', render: (d: number) => `${d} km` },
              { title: '시각 차이', dataIndex: 'time_diff_s', render: (t: number) => `${Math.round(t / 60)}분` }]} />
          <Typography.Text type="secondary">출처: {[...new Set(env.observations.map((o) => o.attribution))].join(' / ')}</Typography.Text>
        </Card>
      ) : env?.warnings?.length ? (
        <Alert type="warning" showIcon message="촬영 당시 환경 정보를 붙이지 않았습니다" description={env.warnings.map((w) => w.message).join(' / ')} />
      ) : null}
      {(isOrg || me.roles.includes('PLATFORM_OPERATOR')) && <ShareCard asset={a} me={me} />}
      <Card title="계보">
        <Table size="small" rowKey="asset_id" pagination={false}
          dataSource={[...(lineage.data?.ancestors ?? []).map((x) => ({ ...x, dir: '원 자산' })), ...(lineage.data?.descendants ?? []).map((x) => ({ ...x, dir: '파생' }))]}
          columns={[{ title: '관계', dataIndex: 'dir' }, { title: '변환', dataIndex: 'relation' },
            { title: '자산', dataIndex: 'asset_id', render: (id: string) => <Link to={`/assets/${id}`}>{id}</Link> }]} />
      </Card>
    </Space>
  )
}
