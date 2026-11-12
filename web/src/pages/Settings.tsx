// 기관 설정 (ORG_ADMIN): 데이터 연계 카드(03번 요구사항 1.5)와 온보딩 마법사(09번 요구사항 3: 확인 필요 항목·정책 초안),
// 웹훅 구독(04번 요구사항 8), 약정 확인(03번 1.4), 긴급 열람 기록(02번 8).
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, App, Button, Card, Checkbox, Form, Input, InputNumber, Modal, Select, Space, Table, Tag, Typography } from 'antd'
import { useState } from 'react'
import type { components } from '../contract/api.gen'
import { api, ApiError, post, type Device, type Me, type Page } from '../lib/api'
import { bytes, kst } from '../lib/format'
import { explain } from '../lib/problems'
import { OnboardingWizard, PolicyDraftList } from './Onboarding'

interface CardContent {
  basic?: { project_name?: string; start_month?: string; [k: string]: unknown }
  current_storage?: { local_copy_policy?: string; local_retention_days?: number; note?: string; [k: string]: unknown }
  collection_mode_requested?: string; integration_methods?: string[]; desired_services?: string[]
  [section: string]: unknown
}
interface CardT {
  integration_card_id: string; version: number; device_id: string | null; created_at: string; created_by: string
  content: CardContent; linked_policies?: { device_id: string; version: number }[]
  open_items: components['schemas']['IntegrationCardOpenItem'][]; difficulty_grade: components['schemas']['DifficultyGrade']
}
type CardForm = Record<string, string | string[] | number | undefined>
// 계약에서 생성한 타입 (09번 작업 16)
type Hook = components['schemas']['WebhookSubscription']
type Agreement = components['schemas']['Agreement']
type Grant = components['schemas']['BreakGlassGrant']
type UsageReport = components['schemas']['UsageReport']

const EVENTS = ['upload.registered', 'upload.quarantined', 'asset.processed', 'share.status_changed', 'grant.revoked', 'incident.opened', 'incident.resolved']
const COPY = ['RETAINED', 'ROTATING', 'NONE', 'UNKNOWN']
const MODES = ['C0_METADATA_ONLY', 'C1_SAMPLE_PREVIEW', 'C2_EVENT_CLIPS', 'C3_APPROVED_FULL']
const NOTES: [string, string][] = [['data', '데이터(영상·센서·로그, 포맷)'], ['network', '통신(망분리·접안 주기)'], ['time_sync', '시각 동기화'],
  ['rights_privacy', '권리·개인정보'], ['samples', '연동 시험 샘플'], ['termination', '종료 시 처리']]

function fail(message: { error: (s: string) => void }) {
  return (e: unknown) => {
    if (e instanceof ApiError) {
      const extra = (e.problem as { errors?: { pointer: string; message: string }[] }).errors?.map((x) => `${x.pointer}: ${x.message}`).join(', ')
      message.error(`${explain(e.code).cause} ${e.problem.detail ?? ''} ${extra ?? ''}`)
    } else message.error(String(e))
  }
}

const str = (x: unknown): string | undefined => (typeof x === 'string' && x ? x : undefined)
const list = (x: unknown): string[] | undefined => (Array.isArray(x) && x.length ? x.map(String) : undefined)

// 이 양식이 다루지 않는 분야(장비·발생량·보고 양식 등)와 항목은 이전 버전 값을 그대로 둔다 (새 버전에서 사라지지 않게)
function toContent(v: CardForm, prev?: CardContent): CardContent {
  const out = JSON.parse(JSON.stringify(prev ?? {})) as CardContent
  const put = (obj: Record<string, unknown>, key: string, val: unknown) => { if (val === undefined) delete obj[key]; else obj[key] = val }
  const section = (key: string, fill: (s: Record<string, unknown>) => void) => {
    const s = { ...((out[key] ?? {}) as Record<string, unknown>) }
    fill(s)
    if (Object.keys(s).length) out[key] = s
    else delete out[key]
  }
  section('basic', (s) => { put(s, 'project_name', str(v.project_name)); put(s, 'start_month', str(v.start_month)) })
  section('current_storage', (s) => {
    put(s, 'local_copy_policy', str(v.local_copy_policy)); put(s, 'note', str(v.storage_note))
    put(s, 'local_retention_days', typeof v.local_retention_days === 'number' ? v.local_retention_days : undefined)
  })
  put(out, 'collection_mode_requested', str(v.collection_mode_requested))
  put(out, 'integration_methods', list(v.integration_methods))
  put(out, 'desired_services', list(v.desired_services)?.slice(0, 3))
  for (const [k] of NOTES) section(k, (s) => put(s, 'note', str(v[k])))
  return out
}

function fromContent(c: CardT | null): CardForm {
  if (!c) return {}
  const x = c.content
  const v: CardForm = { device_id: c.device_id ?? undefined, project_name: x.basic?.project_name, start_month: x.basic?.start_month,
    local_copy_policy: x.current_storage?.local_copy_policy, local_retention_days: x.current_storage?.local_retention_days,
    storage_note: x.current_storage?.note, collection_mode_requested: x.collection_mode_requested,
    integration_methods: x.integration_methods, desired_services: x.desired_services }
  for (const [k] of NOTES) v[k] = str((x[k] as { note?: string } | undefined)?.note)
  return v
}

// 월간 자원 사용량 (08번 요구사항 10): 금액 없이 사용량만
function MonthlyUsage() {
  const [month, setMonth] = useState(() => new Date().toISOString().slice(0, 7))
  const r = useQuery({ queryKey: ['usage-report', month], queryFn: () => api<{ items: UsageReport[] }>(`/v1/usage-reports?month=${month}`) })
  const u = r.data?.items[0]
  return (
    <Card title="월간 자원 사용량" extra={<Input type="month" aria-label="월" value={month} onChange={(e) => e.target.value && setMonth(e.target.value)} style={{ width: 160 }} />} loading={r.isLoading}>
      {u && (
        <Space direction="vertical">
          <Typography.Text>저장 ({u.snapshot_day ?? '스냅숏 없음'} 기준): {Object.entries(u.storage_by_zone).filter(([, v]) => v).map(([z, v]) => `${z} ${bytes(v)}`).join(' · ') || '0'}</Typography.Text>
          <Typography.Text>독립 백업 대상 {bytes(u.backup_target_bytes)} · 신청 백업 {bytes(u.requested_backup_bytes)}</Typography.Text>
          <Typography.Text>처리 시간 CPU {Math.round(u.processing_seconds.CPU / 60)}분 · GPU {Math.round(u.processing_seconds.GPU / 60)}분</Typography.Text>
          <Typography.Text>다운로드 URL {u.downloads.url_count}건 ({bytes(u.downloads.url_bytes)}, 발급 기준) · 데이터셋 배포 {u.downloads.distribution_count}건</Typography.Text>
        </Space>
      )}
    </Card>
  )
}

export function OrgSettings({ me }: { me: Me }) {
  const { message, modal } = App.useApp()
  const qc = useQueryClient()
  const onError = fail(message)
  const cards = useQuery({ queryKey: ['integration-cards'], queryFn: () => api<{ items: CardT[] }>('/v1/integration-cards') })
  const devices = useQuery({ queryKey: ['devices'], queryFn: () => api<Page<Device>>('/v1/devices?limit=500') })
  const hooks = useQuery({ queryKey: ['webhooks'], queryFn: () => api<{ items: Hook[] }>('/v1/webhooks/subscriptions') })
  const agreements = useQuery({ queryKey: ['agreements'], queryFn: () => api<{ items: Agreement[] }>('/v1/agreements') })
  const grants = useQuery({ queryKey: ['break-glass'], queryFn: () => api<{ items: Grant[] }>('/v1/break-glass-grants') })
  const [editing, setEditing] = useState<{ card: CardT | null } | null>(null)
  const [wizard, setWizard] = useState<CardT | null>(null)
  const [addingHook, setAddingHook] = useState(false)

  const saveCard = useMutation({
    mutationFn: (v: CardForm) => {
      const body = { ...(str(v.device_id) ? { device_id: str(v.device_id) } : {}), content: toContent(v, editing?.card?.content) }
      return editing?.card ? post<CardT>(`/v1/integration-cards/${editing.card.integration_card_id}/versions`, body) : post<CardT>('/v1/integration-cards', body)
    },
    onSuccess: (c) => { message.success(`연계 카드 v${c.version} 저장`); setEditing(null); void qc.invalidateQueries({ queryKey: ['integration-cards'] }); void qc.invalidateQueries({ queryKey: ['devices'] }) },
    onError,
  })
  const addHook = useMutation({
    mutationFn: (v: { url: string; event_types: string[] }) => post<Hook>('/v1/webhooks/subscriptions', v),
    onSuccess: (h) => {
      setAddingHook(false)
      void qc.invalidateQueries({ queryKey: ['webhooks'] })
      modal.info({ title: '서명 비밀 (지금 한 번만 보입니다)', width: 560,
        content: <Space direction="vertical"><Typography.Text copyable code>{h.secret}</Typography.Text>
          <Typography.Text type="secondary">수신 서버는 X-Signature = HMAC-SHA256(비밀, 타임스탬프 + "." + 본문)을 확인하고 X-Event-Id로 중복을 거릅니다.</Typography.Text></Space> })
    },
    onError,
  })
  const delHook = useMutation({
    mutationFn: (id: string) => api(`/v1/webhooks/subscriptions/${id}`, { method: 'DELETE' }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ['webhooks'] }) }, onError,
  })
  const deviceName = (id: string | null) => devices.data?.items.find((d) => d.device_id === id)?.name ?? id ?? '-'

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card title="데이터 연계 카드" extra={<Button type="primary" onClick={() => setEditing({ card: null })}>카드 등록</Button>}>
        <Typography.Paragraph type="secondary">장비별 현장 저장 방식·희망 수집 수준 등을 기록합니다. 현장 사본 정책은 장비 기록에 바로 반영되고, 희망 수집 수준은 운영기관이 검토합니다. '정책 초안'으로 장비 정책 초안을 만들면 운영기관이 확인한 뒤 적용합니다. 실제 카드 양식은 확정 전입니다.</Typography.Paragraph>
        <Table rowKey="integration_card_id" size="small" loading={cards.isLoading} dataSource={cards.data?.items ?? []} locale={{ emptyText: '등록된 카드가 없습니다' }} scroll={{ x: 'max-content' }}
          columns={[
            { title: '실증명', key: 'p', render: (_, c) => c.content.basic?.project_name },
            { title: '장비', key: 'd', render: (_, c) => deviceName(c.device_id) },
            { title: '현장 사본', key: 'l', render: (_, c) => c.content.current_storage?.local_copy_policy ?? '-' },
            { title: '희망 수준', key: 'm', render: (_, c) => c.content.collection_mode_requested ?? '-' },
            { title: '버전', dataIndex: 'version', render: (v: number, c) => `v${v} · ${kst(c.created_at)}` },
            { title: '확인 필요', key: 'o', render: (_, c) => (c.open_items.length
              ? <Tag color="orange" title={c.open_items.map((i) => i.label).join(', ')}>{c.open_items.length}개</Tag> : <Tag color="green">없음</Tag>) },
            { title: '연결 정책', key: 'lp', render: (_, c) => (c.linked_policies ?? []).map((x) => <Tag key={`${x.device_id}-${x.version}`}>{deviceName(x.device_id)} v{x.version}</Tag>) },
            { title: '', key: 'a', render: (_, c) => (
              <Space>
                <Button size="small" onClick={() => setEditing({ card: c })}>새 버전</Button>
                <Button size="small" onClick={() => setWizard(c)}>정책 초안</Button>
              </Space>) },
          ]} />
      </Card>

      <PolicyDraftList mode="org" canEdit={me.roles.includes('ORG_ADMIN')} />
      {wizard && <OnboardingWizard card={wizard} onClose={() => setWizard(null)} />}

      <Card title="웹훅 구독" extra={<Button onClick={() => setAddingHook(true)}>구독 추가</Button>}>
        <Table rowKey="webhook_id" size="small" dataSource={hooks.data?.items ?? []} locale={{ emptyText: '구독이 없습니다' }}
          columns={[
            { title: 'URL', dataIndex: 'url', render: (u: string) => <Typography.Text code>{u}</Typography.Text> },
            { title: '이벤트', dataIndex: 'event_types', render: (x: string[]) => x.map((e) => <Tag key={e}>{e}</Tag>) },
            { title: '상태', key: 's', render: (_, h) => h.active ? <Tag color="green">전송 중</Tag> : <Tag color="red" title={h.disabled_reason ?? ''}>중지됨</Tag> },
            { title: '', key: 'a', render: (_, h) => <Button size="small" danger onClick={() => modal.confirm({ title: '구독 삭제', content: '아직 보내지 않은 전송도 취소됩니다.', onOk: () => delHook.mutate(h.webhook_id) })}>삭제</Button> },
          ]} />
        {hooks.data?.items.some((h) => !h.active) && <Alert type="warning" showIcon style={{ marginTop: 8 }} message="전송이 계속 실패해 중지된 구독이 있습니다. 수신 서버를 고친 뒤 새로 등록하세요." />}
      </Card>

      <MonthlyUsage />

      <Card title="약정 (운영기관 기록)">
        <Table rowKey="agreement_id" size="small" pagination={false} dataSource={agreements.data?.items ?? []} locale={{ emptyText: '기록된 약정이 없습니다' }}
          columns={[{ title: '구분', dataIndex: 'tier', render: (t: string) => (t === 'TIER2' ? '2단 공동활용' : '1단 수집·관리') },
            { title: '참조', dataIndex: 'reference' }, { title: '허용 목적', dataIndex: 'allowed_purposes', render: (x: string[]) => x.map((p) => <Tag key={p}>{p}</Tag>) },
            { title: '기간', key: 'v', render: (_, a) => `${kst(a.valid_from)} ~ ${a.valid_until ? kst(a.valid_until) : ''}` },
            { title: '', dataIndex: 'in_effect', render: (x: boolean) => (x ? <Tag color="green">유효</Tag> : <Tag>종료</Tag>) }]} />
      </Card>

      {me.roles.includes('ORG_ADMIN') && (
        <Card title="긴급 열람 기록 (운영기관의 우리 원본 접근)">
          <Table rowKey="grant_id" size="small" dataSource={grants.data?.items ?? []} locale={{ emptyText: '기록이 없습니다' }}
            columns={[{ title: '대상', key: 't', render: (_, g) => `${g.target_type} ${g.target_id}` }, { title: '사유', dataIndex: 'reason' },
              { title: '상태', dataIndex: 'status', render: (s: string) => <Tag color={s === 'ACTIVE' ? 'orange' : undefined}>{s}</Tag> },
              { title: '기간', key: 'p', render: (_, g) => (g.starts_at ? `${kst(g.starts_at)} ~ ${kst(g.expires_at)}` : '-') }]} />
        </Card>
      )}

      <Modal open={!!editing} width={720} title={editing?.card ? `연계 카드 새 버전 (현재 v${editing.card.version})` : '연계 카드 등록'} onCancel={() => setEditing(null)} footer={null} destroyOnClose>
        <Form layout="vertical" onFinish={saveCard.mutate} initialValues={fromContent(editing?.card ?? null)}>
          <Form.Item name="project_name" label="실증명" rules={[{ required: true }]}><Input /></Form.Item>
          <Space wrap>
            <Form.Item name="start_month" label="개시 월 (YYYY-MM)" rules={[{ pattern: /^\d{4}-(0[1-9]|1[0-2])$/ }]}><Input style={{ width: 140 }} /></Form.Item>
            <Form.Item name="device_id" label="장비"><Select allowClear style={{ width: 240 }} options={(devices.data?.items ?? []).map((d) => ({ value: d.device_id, label: d.name }))} /></Form.Item>
            <Form.Item name="local_copy_policy" label="현장 사본 정책"><Select allowClear style={{ width: 160 }} options={COPY.map((x) => ({ value: x, label: x }))} /></Form.Item>
            <Form.Item name="collection_mode_requested" label="희망 수집 수준"><Select allowClear style={{ width: 200 }} options={MODES.map((x) => ({ value: x, label: x }))} /></Form.Item>
            <Form.Item name="local_retention_days" label="현장 보존 기간 (일)"><InputNumber min={0} style={{ width: 140 }} /></Form.Item>
          </Space>
          <Form.Item name="storage_note" label="현행 저장 (위치·기간·백업)"><Input /></Form.Item>
          <Form.Item name="integration_methods" label="연계 방식"><Select mode="tags" placeholder="예: 폴더 에이전트" /></Form.Item>
          <Form.Item name="desired_services" label="원하는 서비스 (최대 3개)" rules={[{ type: 'array', max: 3 }]}><Select mode="tags" /></Form.Item>
          {NOTES.map(([k, label]) => <Form.Item key={k} name={k} label={label}><Input.TextArea rows={2} /></Form.Item>)}
          <Typography.Paragraph type="secondary">담당자 이름·연락처는 적지 않습니다. 필요하면 플랫폼 사용자 계정으로 연결합니다. 아직 모르는 내용은 '확인 필요'라고 적으면 확인 필요 항목에 나옵니다. 이 양식에 없는 분야(장비·발생량 등)는 이전 버전 값을 그대로 둡니다.</Typography.Paragraph>
          <Button type="primary" htmlType="submit" loading={saveCard.isPending}>저장</Button>
        </Form>
      </Modal>

      <Modal open={addingHook} title="웹훅 구독 추가" onCancel={() => setAddingHook(false)} footer={null} destroyOnClose>
        <Form layout="vertical" onFinish={addHook.mutate} initialValues={{ event_types: ['upload.registered', 'upload.quarantined'] }}>
          <Form.Item name="url" label="수신 URL (https)" rules={[{ required: true, pattern: /^https:\/\// }]}><Input placeholder="https://example.org/marine-webhook" /></Form.Item>
          <Form.Item name="event_types" label="이벤트" rules={[{ required: true, type: 'array', min: 1 }]}>
            <Checkbox.Group options={EVENTS.map((e) => ({ value: e, label: e }))} />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={addHook.isPending}>등록</Button>
        </Form>
      </Modal>
    </Space>
  )
}
