// 자산 탐색·상세·영상 뷰어 (09번 요구사항 6, 7).
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Card, Descriptions, Select, Space, Table, Tag, Typography } from 'antd'
import { useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { api, ApiError, post, type Asset, type Device, type Me, type Page, type Session } from '../lib/api'
import { bytes, kst, QUARANTINE_LABEL, SCOPE_LABEL, STATUS_LABEL } from '../lib/format'
import { explain } from '../lib/problems'
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
    <Card title="자산">
      <Space wrap style={{ marginBottom: 16 }}>
        {sel('session_id', '회차', (sessions.data?.items ?? []).map((s) => ({ value: s.session_id, label: s.label })))}
        {sel('source_system_id', '장비', (devices.data?.items ?? []).filter((d) => d.kind === 'SOURCE').map((d) => ({ value: d.device_id, label: d.name })))}
        {sel('data_type', '데이터 유형', ['VIDEO', 'IMAGE', 'SENSOR', 'DOCUMENT', 'LOG'].map((v) => ({ value: v, label: v })))}
        {sel('variant', '변형', ['ORIGINAL', 'CLIP', 'PROXY', 'THUMBNAIL'].map((v) => ({ value: v, label: v })))}
        {sel('quality_status', '품질', ['PASSED', 'WARNING', 'FAILED', 'PENDING'].map((v) => ({ value: v, label: v })))}
        {sel('ingest_status', '상태', Object.entries(STATUS_LABEL).map(([value, label]) => ({ value, label })))}
        {sel('sharing_scope', '공유 등급', Object.entries(SCOPE_LABEL).map(([value, label]) => ({ value, label })))}
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
  )
}

interface Lineage { descendants: { asset_id: string; relation: string }[]; ancestors: { asset_id: string; relation: string }[] }

export function AssetDetail({ me }: { me: Me }) {
  const { assetId = '' } = useParams()
  const asset = useQuery({ queryKey: ['asset', assetId], queryFn: () => api<Asset>(`/v1/assets/${assetId}`) })
  const lineage = useQuery({ queryKey: ['lineage', assetId], queryFn: () => api<Lineage>(`/v1/assets/${assetId}/lineage`) })
  const [video, setVideo] = useState<string | null>(null)
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
            <video src={video} controls style={{ maxWidth: '100%' }} aria-label="프록시 영상" />
          ) : (
            <Button type="primary" onClick={() => void play(proxy.asset_id)}>재생</Button>
          )}
          <Typography.Paragraph type="secondary">구간 클립 내보내기는 준비 중입니다 (09번 요구사항 7.2).</Typography.Paragraph>
        </Card>
      )}
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
