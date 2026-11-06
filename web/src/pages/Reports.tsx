// 실증 기록·보고서 (10번): 회차 기록 작성·승인, 보고서 초안·수정·승인·제출, 내보내기, 지표 근거 추적.
// 관리기관 열람자는 제출된 보고서만 본다 (같은 화면, 읽기 전용).
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, App, Button, Card, Descriptions, Drawer, Form, Input, Modal, Select, Space, Table, Tag, Typography } from 'antd'
import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, ApiError, patch, post, type Capabilities, type Me, type Page, type Session } from '../lib/api'
import { currentUser } from '../lib/auth'
import { kst } from '../lib/format'
import { explain } from '../lib/problems'

interface Metric {
  metric_code: string; metric_version: number; name: string; unit: string | null; status: 'OK' | 'NO_DATA' | 'NEEDS_VERIFICATION'
  value: number | null; formula: string; input_count: number; missing_count: number
  sources: { event_ids: string[]; asset_ids: string[]; observation_ranges: unknown[] }
}
interface Field { key: string; label: string; auto_value: string | null; value: string | null; edited: boolean; history?: { previous: string | null; value: string | null; edited_by: string; edited_at: string }[] }
interface Report {
  report_id: string; title: string; version: number; latest_version: number; status: string; session_id: string | null
  generated_at: string; data_as_of: string; fields: Field[]; metrics: Metric[]; record_ids: string[]
  evidence_damaged: boolean; damage?: { asset_id: string; reason: string }[]; snapshot_sha256: string | null
}
interface RecordV { record_id: string; title: string; kind: string; version: number; status: string; result: string | null; evidence_refs: unknown[]; evidence_damaged: boolean }
interface Trace {
  records: { record_id: string; version: number; title: string }[]
  events: { event_id: string; event_code: string; source: string; human_verified: boolean; asset_ids: string[] }[]
  assets: { asset_id: string; variant: string; ingest_status: string; originals: string[] }[]
  observation_ranges: unknown[]
  hidden_count: number
}

const STATUS: Record<string, { color: string; label: string }> = {
  DRAFT: { color: 'blue', label: '초안' }, APPROVED: { color: 'green', label: '승인' }, SUBMITTED: { color: 'purple', label: '제출' },
  SUPERSEDED: { color: 'default', label: '대체됨' },
}
const METRIC: Record<string, { color: string; label: string }> = {
  OK: { color: 'green', label: '확정' }, NO_DATA: { color: 'default', label: '데이터 없음' }, NEEDS_VERIFICATION: { color: 'orange', label: '검증 필요' },
}
const metricValue = (m: Metric) => (m.value == null ? '-' : `${m.value}${m.unit ? ` ${m.unit}` : ''}`)

function useFail() {
  const { message } = App.useApp()
  return (e: unknown) => message.error(e instanceof ApiError ? `${explain(e.code).cause} ${e.problem.detail ?? ''}` : String(e))
}

export function Reports({ me }: { me: Me }) {
  const qc = useQueryClient()
  const fail = useFail()
  const isOrg = me.roles.some((r) => r.startsWith('ORG_'))
  const reports = useQuery({ queryKey: ['reports'], queryFn: () => api<Page<Report>>('/v1/reports?limit=200') })
  const sessions = useQuery({ queryKey: ['sessions'], queryFn: () => api<Page<Session>>('/v1/sessions?limit=500'), enabled: isOrg })
  const templates = useQuery({ queryKey: ['report-templates'], queryFn: () => api<{ items: { code: string; version: number; name: string; scope: string }[] }>('/v1/report-templates'), enabled: isOrg })
  const [open, setOpen] = useState(false)
  const create = useMutation({
    mutationFn: (v: { template_code: string; session_id: string }) => post<Report>('/v1/reports', v),
    onSuccess: () => { setOpen(false); void qc.invalidateQueries({ queryKey: ['reports'] }) },
    onError: fail,
  })
  const latestTemplates = Object.values(Object.fromEntries((templates.data?.items ?? []).filter((t) => t.scope === 'SESSION').map((t) => [t.code, t])))
  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card title={isOrg ? '보고서' : '제출된 보고서'} extra={isOrg && <Button type="primary" onClick={() => setOpen(true)}>회차 보고서 초안 만들기</Button>}>
        <Table rowKey="report_id" size="small" loading={reports.isLoading} dataSource={reports.data?.items ?? []}
          locale={{ emptyText: isOrg ? '보고서가 없습니다' : '제출된 보고서가 없습니다' }}
          columns={[
            { title: '제목', key: 't', render: (_, r) => <Link to={`/reports/${r.report_id}`}>{r.title}</Link> },
            { title: '버전', dataIndex: 'version' },
            { title: '상태', dataIndex: 'status', render: (s: string) => <Tag color={STATUS[s]?.color}>{STATUS[s]?.label ?? s}</Tag> },
            { title: '근거', key: 'd', render: (_, r) => (r.evidence_damaged ? <Tag color="red">근거 손상</Tag> : '-') },
            { title: '데이터 기준', dataIndex: 'data_as_of', render: kst },
          ]} />
      </Card>
      <Modal open={open} title="회차 보고서 초안" onCancel={() => setOpen(false)} footer={null} destroyOnClose>
        <Form layout="vertical" onFinish={create.mutate}>
          <Form.Item name="template_code" label="양식" rules={[{ required: true }]}>
            <Select virtual={false} options={latestTemplates.map((t) => ({ value: t.code, label: `${t.name} (v${t.version})` }))} />
          </Form.Item>
          <Form.Item name="session_id" label="실증회차" rules={[{ required: true }]}>
            <Select virtual={false} options={(sessions.data?.items ?? []).map((s) => ({ value: s.session_id, label: `${s.label} ${s.title}` }))} />
          </Form.Item>
          <Typography.Paragraph type="secondary">지표는 지금 데이터로 계산하고 근거(이벤트·자산·관측 구간)를 함께 저장합니다. 승인된 회차 기록만 들어갑니다.</Typography.Paragraph>
          <Button type="primary" htmlType="submit" loading={create.isPending}>만들기</Button>
        </Form>
      </Modal>
    </Space>
  )
}

export function ReportDetail({ me }: { me: Me }) {
  const { reportId = '' } = useParams()
  const qc = useQueryClient()
  const fail = useFail()
  const { message } = App.useApp()
  const isOrg = me.roles.some((r) => r.startsWith('ORG_'))
  const isAdmin = me.roles.includes('ORG_ADMIN')
  const report = useQuery({ queryKey: ['report', reportId], queryFn: () => api<Report>(`/v1/reports/${reportId}`) })
  const { data: caps } = useQuery({ queryKey: ['capabilities'], queryFn: () => api<Capabilities>('/v1/system/capabilities') })
  const [trace, setTrace] = useState<{ metric: Metric; data?: Trace } | null>(null)
  const [edit, setEdit] = useState<Field | null>(null)
  const refresh = () => { void qc.invalidateQueries({ queryKey: ['report', reportId] }); void qc.invalidateQueries({ queryKey: ['reports'] }) }
  const act = useMutation({
    mutationFn: (path: string) => post<Report>(`/v1/reports/${reportId}/${path}`, {}),
    onSuccess: (r) => { message.success(`${STATUS[r.status]?.label ?? r.status} 처리했습니다`); refresh() },
    onError: fail,
  })
  const save = useMutation({
    mutationFn: (v: { key: string; value: string | null }) => patch<Report>(`/v1/reports/${reportId}/fields`, { fields: [v] }),
    onSuccess: () => { setEdit(null); refresh() },
    onError: fail,
  })
  const r = report.data
  if (!r) return <Card loading={report.isLoading}>{report.error ? '보고서를 찾을 수 없습니다' : null}</Card>
  const damageList = r.damage?.map((d) => `${d.asset_id} (${d.reason})`).join(', ')
  const damageDescription = damageList ? `인용한 자산이 바뀌었습니다: ${damageList}.` : '인용한 근거가 변경되었습니다.'
  const openTrace = async (m: Metric) => {
    setTrace({ metric: m })
    try {
      setTrace({ metric: m, data: await api<Trace>(`/v1/reports/${reportId}/metrics/${m.metric_code}/trace?version=${r.version}`) })
    } catch (e) { fail(e) }
  }
  const download = async (format: string) => {
    const user = await currentUser()
    const res = await fetch(`/v1/reports/${reportId}/export?format=${format}&version=${r.version}`, { headers: { Authorization: `Bearer ${user?.access_token ?? ''}` } })
    if (!res.ok) return message.error('내보내기에 실패했습니다')
    const a = document.createElement('a')
    a.href = URL.createObjectURL(await res.blob())
    a.download = `${r.report_id}-v${r.version}.${format.toLowerCase()}`
    a.click()
  }
  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      {r.evidence_damaged && (
        <Alert type="error" showIcon message={isOrg ? '근거 손상 — 제출할 수 없습니다' : '근거 손상'}
          description={isOrg ? `${damageDescription} 근거를 고친 뒤 새 버전을 만드세요.` : damageDescription} />
      )}
      <Card title={r.title} extra={
        <Space wrap>
          {(caps?.report_formats ?? []).map((f) => <Button key={f} onClick={() => void download(f)}>{f}</Button>)}
          {isOrg && <Button onClick={() => act.mutate('versions')}>새 버전(다시 계산)</Button>}
          {isAdmin && r.status === 'DRAFT' && <Button type="primary" onClick={() => act.mutate('approve')}>승인</Button>}
          {isAdmin && r.status === 'APPROVED' && <Button type="primary" disabled={r.evidence_damaged} onClick={() => act.mutate('submit')}>관리기관에 제출</Button>}
        </Space>
      }>
        <Descriptions size="small" column={{ xs: 1, md: 3 }}>
          <Descriptions.Item label="상태"><Tag color={STATUS[r.status]?.color}>{STATUS[r.status]?.label}</Tag> 버전 {r.version} / {r.latest_version}</Descriptions.Item>
          <Descriptions.Item label="생성">{kst(r.generated_at)}</Descriptions.Item>
          <Descriptions.Item label="데이터 기준">{kst(r.data_as_of)}</Descriptions.Item>
          {r.snapshot_sha256 && <Descriptions.Item label="승인본 sha256"><Typography.Text code copyable>{r.snapshot_sha256.slice(0, 16)}…</Typography.Text></Descriptions.Item>}
        </Descriptions>
      </Card>
      <Card title="지표 (행을 누르면 근거를 거슬러 올라갑니다)">
        <Table rowKey="metric_code" size="small" pagination={false} dataSource={r.metrics}
          onRow={(m) => ({ onClick: () => void openTrace(m), style: { cursor: 'pointer' } })}
          columns={[
            // 행 누르기는 마우스용. 키보드로도 열 수 있게 지표 이름을 버튼으로 (요구사항 11.2)
            { title: '지표', key: 'n', render: (_, m) => (
              <Button type="link" size="small" style={{ padding: 0, height: 'auto', whiteSpace: 'normal', textAlign: 'left' }}
                aria-label={`${m.name} (v${m.metric_version}) 근거 보기`} onClick={(e) => { e.stopPropagation(); void openTrace(m) }}>
                {m.name} (v{m.metric_version})
              </Button>
            ) },
            { title: '값', key: 'v', render: (_, m) => (m.status === 'OK' ? metricValue(m) : <Typography.Text type="secondary">{metricValue(m)}</Typography.Text>) },
            { title: '상태', dataIndex: 'status', render: (s: string) => <Tag color={METRIC[s].color}>{METRIC[s].label}</Tag> },
            { title: '계산식', dataIndex: 'formula' },
            { title: '입력·누락', key: 'c', render: (_, m) => `${m.input_count} / ${m.missing_count}` },
          ]} />
        <Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>
          검증 필요: 모델 결과만으로 계산된 값입니다. 확정 성능값으로 내보내지 않습니다. 데이터 없음: 값을 추정하지 않았습니다.
        </Typography.Paragraph>
      </Card>
      <Card title="작성 항목">
        <Table rowKey="key" size="small" pagination={false} dataSource={r.fields}
          columns={[
            { title: '항목', dataIndex: 'label' },
            { title: '값', key: 'v', render: (_, f) => <span>{f.value ?? '-'} {f.edited && <Tag color="blue">수정됨</Tag>}</span> },
            { title: '자동값', dataIndex: 'auto_value', render: (v: string | null) => v ?? '-' },
            { title: '', key: 'e', render: (_, f) => isOrg && r.status === 'DRAFT' && <Button size="small" onClick={() => setEdit(f)}>고치기</Button> },
          ]} />
      </Card>
      <Modal open={!!edit} title={edit?.label} onCancel={() => setEdit(null)} footer={null} destroyOnClose>
        {edit && (
          <Form layout="vertical" initialValues={{ value: edit.value ?? '' }} onFinish={(v: { value: string }) => save.mutate({ key: edit.key, value: v.value })}>
            <Form.Item name="value" label="값"><Input.TextArea rows={4} /></Form.Item>
            <Typography.Paragraph type="secondary">자동값: {edit.auto_value ?? '-'}</Typography.Paragraph>
            {(edit.history ?? []).map((h, i) => (
              <Typography.Paragraph key={i} type="secondary">{kst(h.edited_at)} {h.edited_by}: "{h.previous ?? ''}" → "{h.value ?? ''}"</Typography.Paragraph>
            ))}
            <Space>
              <Button type="primary" htmlType="submit" loading={save.isPending}>저장</Button>
              <Button onClick={() => save.mutate({ key: edit.key, value: null })}>자동값으로 되돌리기</Button>
            </Space>
          </Form>
        )}
      </Modal>
      <Drawer open={!!trace} width={560} title={trace && `근거 경로 — ${trace.metric.name}`} onClose={() => setTrace(null)}>
        {trace?.data && (
          <Space direction="vertical" style={{ width: '100%' }}>
            <Typography.Text strong>회차 기록</Typography.Text>
            {trace.data.records.length ? trace.data.records.map((x) => <div key={x.record_id}>{x.title} (v{x.version})</div>) : <Typography.Text type="secondary">연결된 기록 없음</Typography.Text>}
            <Typography.Text strong>이벤트</Typography.Text>
            {trace.data.events.map((e) => <div key={e.event_id}>{e.event_code} · {e.source}{!e.human_verified && e.source === 'MODEL' ? ' (미확인)' : ''}</div>)}
            <Typography.Text strong>관측 구간</Typography.Text>
            {(trace.data.observation_ranges as { source_system_id: string; metric: string; from: string; to: string; count: number }[]).map((o) => (
              <div key={`${o.source_system_id}:${o.metric}`}>{o.metric} · {o.source_system_id} · {kst(o.from)} ~ {kst(o.to)} ({o.count}건)</div>
            ))}
            <Typography.Text strong>자산 → 원본</Typography.Text>
            {trace.data.assets.map((a) => (
              <div key={a.asset_id}>{isOrg ? <Link className="inline-link" to={`/assets/${a.asset_id}`}>{a.asset_id}</Link> : a.asset_id} ({a.variant}) → {a.originals.join(', ')}</div>
            ))}
            {trace.data.hidden_count > 0 && <Typography.Text type="secondary">권한 범위 밖 근거 {trace.data.hidden_count}건은 숨겼습니다</Typography.Text>}
          </Space>
        )}
      </Drawer>
    </Space>
  )
}

export function SessionRecords({ me }: { me: Me }) {
  const { sessionId = '' } = useParams()
  const qc = useQueryClient()
  const fail = useFail()
  const isAdmin = me.roles.includes('ORG_ADMIN')
  const records = useQuery({ queryKey: ['records', sessionId], queryFn: () => api<{ items: RecordV[] }>(`/v1/sessions/${sessionId}/records`) })
  const assets = useQuery({ queryKey: ['assets', 'session', sessionId], queryFn: () => api<Page<{ asset_id: string; variant: string; provenance: { original_filename?: string } }>>(`/v1/assets?session_id=${sessionId}&limit=200`) })
  const [open, setOpen] = useState(false)
  const refresh = () => void qc.invalidateQueries({ queryKey: ['records', sessionId] })
  const create = useMutation({
    mutationFn: (v: { kind: string; title: string; test_item?: string; conditions?: string; result?: string; note?: string; assets?: string[] }) =>
      post(`/v1/sessions/${sessionId}/records`, {
        kind: v.kind, title: v.title, ...Object.fromEntries(Object.entries({ test_item: v.test_item, conditions: v.conditions, result: v.result, note: v.note }).filter(([, x]) => x)),
        evidence_refs: (v.assets ?? []).map((asset_id) => ({ kind: 'ASSET', asset_id })),
      }),
    onSuccess: () => { setOpen(false); refresh() },
    onError: fail,
  })
  const approve = useMutation({ mutationFn: (id: string) => post(`/v1/records/${id}/approve`, {}), onSuccess: refresh, onError: fail })
  return (
    <Card title="회차 기록 (실증일지·시험 결과)" extra={<Button type="primary" onClick={() => setOpen(true)}>기록 작성</Button>}>
      <Table rowKey="record_id" size="small" loading={records.isLoading} dataSource={records.data?.items ?? []} locale={{ emptyText: '기록이 없습니다' }}
        columns={[
          { title: '제목', dataIndex: 'title' },
          { title: '종류', dataIndex: 'kind' },
          { title: '결과', dataIndex: 'result' },
          { title: '근거 수', key: 'e', render: (_, x) => x.evidence_refs.length },
          { title: '버전', key: 'v', render: (_, x) => <span>v{x.version} <Tag color={x.status === 'APPROVED' ? 'green' : 'blue'}>{x.status === 'APPROVED' ? '승인' : '초안'}</Tag>{x.evidence_damaged && <Tag color="red">근거 손상</Tag>}</span> },
          { title: '', key: 'a', render: (_, x) => isAdmin && x.status !== 'APPROVED' && <Button size="small" onClick={() => approve.mutate(x.record_id)}>증빙 승인</Button> },
        ]} />
      <Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>승인한 버전은 바뀌지 않고 인용 자산은 독립 백업 대상(PROTECTED)이 됩니다. 고치면 새 버전이 생깁니다.</Typography.Paragraph>
      <Modal open={open} title="회차 기록 작성" onCancel={() => setOpen(false)} footer={null} destroyOnClose>
        <Form layout="vertical" onFinish={create.mutate} initialValues={{ kind: 'LOG' }}>
          <Form.Item name="kind" label="종류"><Select virtual={false} options={[{ value: 'LOG', label: '실증일지' }, { value: 'TEST', label: '시험 결과' }, { value: 'NOTE', label: '메모' }]} /></Form.Item>
          <Form.Item name="title" label="제목" rules={[{ required: true, max: 200 }]}><Input /></Form.Item>
          <Form.Item name="test_item" label="시험 항목"><Input /></Form.Item>
          <Form.Item name="conditions" label="조건"><Input.TextArea rows={2} /></Form.Item>
          <Form.Item name="result" label="결과"><Input.TextArea rows={2} /></Form.Item>
          <Form.Item name="note" label="메모"><Input.TextArea rows={2} /></Form.Item>
          <Form.Item name="assets" label="근거 자산">
            <Select virtual={false} mode="multiple" options={(assets.data?.items ?? []).map((a) => ({ value: a.asset_id, label: `${a.provenance.original_filename ?? a.asset_id} (${a.variant})` }))} />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={create.isPending}>저장</Button>
        </Form>
      </Modal>
    </Card>
  )
}
