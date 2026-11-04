// 공유·데이터셋·배포 화면 (13번 요구사항 13): 자산 공유 카드, 운영자 심사·데이터셋, 이용기관 카탈로그, 제공기관 승인·이력.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, App, Button, Card, Form, Input, Modal, Select, Space, Table, Tag, Typography } from 'antd'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api, ApiError, post, type Asset, type Me, type Page } from '../lib/api'
import { bytes, kst, SCOPE_LABEL } from '../lib/format'
import { explain } from '../lib/problems'

interface Unmet { condition: string; actual: string | null }
interface Review {
  asset_id: string; share_status: string; legal_basis_ref: string | null; max_scope: string
  derived: { asset_id: string; variant: string; share_status: string; review_result: string | null; privacy_status: string; sharing_scope: string }[]
  events: { from_status: string; to_status: string; actor: string; reason: string | null; at: string }[]
}
interface DatasetT { dataset_id: string; title: string; published: boolean; versions: { dataset_version_id: string; version: string; scope: string; item_count: number; total_bytes: number; created_at: string }[]; data_card?: Record<string, unknown> }
interface AccessReq { access_request_id: string; dataset_version_id: string; purpose: string; status: string; pending_provider_approvals: number; deadline_at: string; grant_id?: string; grant_status?: string; grant_expires_at?: string }
interface LineageT {
  dataset_version_id: string; version: string; manifest_sha256: string; hidden_item_count: number
  items: { asset_id: string; path: string; organization_id: string; source_asset_id: string; chain: { asset_id: string; relation: string; variant: string }[]
    processing: { model: { name?: string; version?: string; sha256?: string | null; license?: string | null }; processed_at: string | null } | null
    reviews: { review_id: string; reviewer_id: string; status: string; recheck?: boolean; decided_at: string | null }[]; review_result: string | null }[]
}
interface GrantT { grant_id: string; dataset_version_id: string; status: string; expires_at: string; notice?: string; destruction_confirmed_at: string | null }

const SHARE_LABEL: Record<string, string> = {
  NONE: '공유 안 함', CANDIDATE: '후보', LEGAL_REVIEW: '법률 검토', PROCESSING: '처리 중', QUALITY_REVIEW: '검수',
  APPROVED: '승인', REJECTED: '반려', REPROCESS: '재처리',
}
const PURPOSES = ['JOINT_RESEARCH', 'AI_TRAINING', 'MODEL_IMPROVEMENT', 'COMMERCIAL_SERVICE', 'EXTERNAL_SALE']

function useFail() {
  const { message, modal } = App.useApp()
  return (e: unknown) => {
    if (e instanceof ApiError && e.code === 'RULE_VIOLATION') {
      const unmet = ((e.problem as { unmet_conditions?: Unmet[] }).unmet_conditions ?? [])
      modal.warning({ title: '조건을 충족하지 못했습니다', content: <ul>{unmet.map((u, i) => <li key={i}>{u.condition} (현재: {u.actual ?? '-'})</li>)}</ul> })
      return
    }
    message.error(e instanceof ApiError ? `${explain(e.code).cause} ${e.problem.detail ?? ''}` : String(e))
  }
}

// 자산 상세의 공유 카드: 원본은 후보 지정·심사 이력, 파생본은 등급 변경 신청 (ORG_ADMIN)
export function ShareCard({ asset, me }: { asset: Asset & { share_status?: string }; me: Me }) {
  const qc = useQueryClient()
  const fail = useFail()
  const { message } = App.useApp()
  const isAdmin = me.roles.includes('ORG_ADMIN')
  const isOrg = me.roles.some((r) => r.startsWith('ORG_'))
  const original = asset.variant === 'ORIGINAL'
  const review = useQuery({ queryKey: ['share-review', asset.asset_id], queryFn: () => api<Review>(`/v1/assets/${asset.asset_id}/share-review`), enabled: original })
  const refresh = () => { void qc.invalidateQueries({ queryKey: ['share-review', asset.asset_id] }); void qc.invalidateQueries({ queryKey: ['asset', asset.asset_id] }) }
  const candidate = useMutation({
    mutationFn: () => post<{ candidates: string[]; excluded: { reason: string }[] }>('/v1/share-candidates', { asset_ids: [asset.asset_id] }),
    onSuccess: (r) => { message.info(r.candidates.length ? '공유 후보로 지정했습니다' : `제외: ${r.excluded.map((x) => x.reason).join(', ')}`); refresh() },
    onError: fail,
  })
  const [target, setTarget] = useState<string>()
  const request = useMutation({
    mutationFn: (v: { reason: string; existing_grant_handling?: string }) => post<{ state: string }>(`/v1/assets/${asset.asset_id}/share-requests`, { target_scope: target, ...v }),
    onSuccess: (r) => { message.success(r.state === 'IN_REVIEW' ? '운영자 승인 대기' : '반영했습니다'); refresh() },
    onError: fail,
  })
  return (
    <Card title="공유">
      <Space direction="vertical" style={{ width: '100%' }}>
        <Typography.Text>현재 등급 <Tag>{SCOPE_LABEL[asset.sharing_scope]}</Tag> 역할 {asset.data_role} · 심사 {SHARE_LABEL[asset.share_status ?? 'NONE']}</Typography.Text>
        {original && (
          <>
            {isOrg && asset.data_role === 'BYPRODUCT' && (asset.share_status ?? 'NONE') === 'NONE' && (
              <Button onClick={() => candidate.mutate()}>공유 후보로 지정</Button>
            )}
            <Typography.Paragraph type="secondary">원본은 L1(관리기관 검토)을 넘지 않습니다. 공유는 심사를 거친 비식별본 등 파생본으로 합니다.</Typography.Paragraph>
            {review.data && review.data.events.length > 0 && (
              <Table size="small" pagination={false} rowKey="at" dataSource={review.data.events}
                columns={[{ title: '시각', dataIndex: 'at', render: kst }, { title: '단계', key: 's', render: (_, e) => `${SHARE_LABEL[e.from_status]} → ${SHARE_LABEL[e.to_status]}` },
                  { title: '주체', dataIndex: 'actor' }, { title: '사유', dataIndex: 'reason' }]} />
            )}
          </>
        )}
        {isAdmin && (
          <Form layout="inline" onFinish={request.mutate}>
            <Form.Item><Select virtual={false} style={{ width: 200 }} placeholder="목표 등급" aria-label="목표 등급" value={target} onChange={setTarget}
              options={Object.entries(SCOPE_LABEL).filter(([k]) => k !== asset.sharing_scope).map(([value, label]) => ({ value, label }))} /></Form.Item>
            <Form.Item name="reason" rules={[{ required: true, min: 2 }]}><Input placeholder="사유" aria-label="사유" /></Form.Item>
            <Form.Item name="existing_grant_handling"><Select virtual={false} allowClear style={{ width: 200 }} placeholder="하향 시 기존 권한" aria-label="하향 시 기존 권한"
              options={[{ value: 'REVOKE_NOW', label: '즉시 철회' }, { value: 'KEEP_UNTIL_EXPIRY', label: '만료까지 유지' }]} /></Form.Item>
            <Button htmlType="submit" disabled={!target} loading={request.isPending}>등급 변경 신청</Button>
          </Form>
        )}
      </Space>
    </Card>
  )
}

// 운영자: 심사 대기 원본, 데이터셋 구성·게시, 이용 신청 최종 승인
export function SharingConsole() {
  const qc = useQueryClient()
  const fail = useFail()
  const statuses = ['CANDIDATE', 'LEGAL_REVIEW', 'QUALITY_REVIEW']
  const queue = useQuery({
    queryKey: ['share-queue'],
    queryFn: async () => (await Promise.all(statuses.map((s) => api<Page<Asset & { share_status: string; organization_id?: string }>>(`/v1/assets?share_status=${s}&limit=200`)))).flatMap((p) => p.items),
  })
  const sets = useQuery({ queryKey: ['datasets'], queryFn: () => api<{ items: DatasetT[] }>('/v1/datasets') })
  const requests = useQuery({ queryKey: ['access-requests'], queryFn: () => api<{ items: AccessReq[] }>('/v1/access-requests') })
  const refresh = () => ['share-queue', 'datasets', 'access-requests'].forEach((k) => void qc.invalidateQueries({ queryKey: [k] }))
  const act = useMutation({
    mutationFn: ({ id, action, legal_basis_ref, reason }: { id: string; action: string; legal_basis_ref?: string; reason?: string }) =>
      post(`/v1/assets/${id}/share-review`, { action, ...(legal_basis_ref ? { legal_basis_ref } : {}), ...(reason ? { reason } : {}) }),
    onSuccess: refresh, onError: fail,
  })
  const createDs = useMutation({ mutationFn: (title: string) => post('/v1/datasets', { title }), onSuccess: refresh, onError: fail })
  const [versionFor, setVersionFor] = useState<DatasetT | null>(null)
  const addVersion = useMutation({
    mutationFn: (v: { version: string; items: string; card: string }) => post(`/v1/datasets/${versionFor?.dataset_id}/versions`, {
      version: v.version, item_asset_ids: v.items.split(/[\s,]+/).filter(Boolean), data_card: JSON.parse(v.card),
    }),
    onSuccess: () => { setVersionFor(null); refresh() }, onError: fail,
  })
  const publish = useMutation({ mutationFn: (id: string) => post(`/v1/datasets/${id}/publish`, {}), onSuccess: refresh, onError: fail })
  const decide = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: string }) => post(`/v1/access-requests/${id}/decisions`, { decision, reason: decision === 'APPROVE' ? '이용자 약정 확인' : '운영자 거절' }),
    onSuccess: refresh, onError: fail,
  })
  const [legal, setLegal] = useState('')
  const [lineageOf, setLineageOf] = useState<{ ds: string; vid: string } | null>(null)
  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card title="공유 심사 대기 (원본 단위)">
        <Input style={{ maxWidth: 360, marginBottom: 8 }} placeholder="법률 검토 근거(legal_basis_ref)" aria-label="법률 검토 근거" value={legal} onChange={(e) => setLegal(e.target.value)} />
        <Table rowKey="asset_id" size="small" loading={queue.isLoading} dataSource={queue.data ?? []} locale={{ emptyText: '심사 대기가 없습니다' }}
          columns={[
            { title: '자산', key: 'a', render: (_, a) => <Link to={`/assets/${a.asset_id}`}>{a.provenance.original_filename ?? a.asset_id}</Link> },
            { title: '단계', dataIndex: 'share_status', render: (s: string) => <Tag>{SHARE_LABEL[s]}</Tag> },
            { title: '', key: 'x', render: (_, a) => (
              <Space wrap>
                {a.share_status === 'CANDIDATE' && <Button size="small" onClick={() => act.mutate({ id: a.asset_id, action: 'START_LEGAL_REVIEW' })}>법률 검토 시작</Button>}
                {a.share_status === 'LEGAL_REVIEW' && <Button size="small" disabled={!legal} onClick={() => act.mutate({ id: a.asset_id, action: 'PASS_LEGAL_REVIEW', legal_basis_ref: legal })}>법률 검토 통과</Button>}
                {a.share_status === 'QUALITY_REVIEW' && <Button size="small" type="primary" onClick={() => act.mutate({ id: a.asset_id, action: 'APPROVE' })}>최종 승인</Button>}
                <Button size="small" danger onClick={() => act.mutate({ id: a.asset_id, action: 'REJECT', reason: '운영자 반려' })}>반려</Button>
              </Space>
            ) },
          ]} />
      </Card>
      <Card title="데이터셋" extra={<Button onClick={() => { const t = window.prompt('데이터셋 이름'); if (t) createDs.mutate(t) }}>데이터셋 만들기</Button>}>
        <Table rowKey="dataset_id" size="small" dataSource={sets.data?.items ?? []}
          columns={[
            { title: '이름', dataIndex: 'title' },
            { title: '버전 (눌러서 계보)', key: 'v', render: (_, d) => d.versions.map((v) => (
              <Button key={v.dataset_version_id} size="small" type="link" aria-label={`${v.version} 계보 보기`} onClick={() => setLineageOf({ ds: d.dataset_id, vid: v.dataset_version_id })}>
                {v.version} · {SCOPE_LABEL[v.scope]} · {v.item_count}개
              </Button>)) },
            { title: '게시', dataIndex: 'published', render: (p: boolean) => (p ? <Tag color="green">게시</Tag> : '-') },
            { title: '', key: 'a', render: (_, d) => (
              <Space>
                <Button size="small" onClick={() => setVersionFor(d)}>새 버전</Button>
                {!d.published && d.versions.length > 0 && <Button size="small" onClick={() => publish.mutate(d.dataset_id)}>게시</Button>}
              </Space>
            ) },
          ]} />
      </Card>
      <Card title="이용 신청 최종 승인 (제공기관 모두 승인한 것)">
        <Table rowKey="access_request_id" size="small" dataSource={(requests.data?.items ?? [])}
          columns={[
            { title: '신청', dataIndex: 'access_request_id' }, { title: '목적', dataIndex: 'purpose' },
            { title: '상태', dataIndex: 'status', render: (s: string) => <Tag>{s}</Tag> },
            { title: '제공기관 대기', dataIndex: 'pending_provider_approvals' },
            { title: '기한', dataIndex: 'deadline_at', render: kst },
            { title: '', key: 'a', render: (_, r) => r.status === 'PROVIDER_APPROVED' && (
              <Space><Button size="small" type="primary" onClick={() => decide.mutate({ id: r.access_request_id, decision: 'APPROVE' })}>승인</Button>
                <Button size="small" danger onClick={() => decide.mutate({ id: r.access_request_id, decision: 'DENY' })}>거절</Button></Space>
            ) },
          ]} />
      </Card>
      <LineageModal target={lineageOf} onClose={() => setLineageOf(null)} />
      <Modal open={!!versionFor} width={720} title={`새 버전 — ${versionFor?.title ?? ''}`} onCancel={() => setVersionFor(null)} footer={null} destroyOnClose>
        <Form layout="vertical" onFinish={addVersion.mutate} initialValues={{ card: JSON.stringify({ title: versionFor?.title ?? '', rights_holders: [], review_owner: '', source_summary: { organization_count: 1, session_count: 0, period_start: '', period_end: '', sea_areas_generalized: [] }, contents: { file_count: 1, total_bytes: 0, formats: [] }, conditions: { missing_data_note: '', bias_note: '', limitations: '' }, privacy: { risk_note: '', processing_methods: [], review_method: '' }, lineage: { model_versions: [] }, usage: { allowed_purposes: ['AI_TRAINING'], redistribution_allowed: false, commercial_use_allowed: false, revocation_terms: '' }, delivery: { methods: ['DOWNLOAD_URL'] } }, null, 2) }}>
          <Form.Item name="version" label="버전 (예: 1.0.0)" rules={[{ required: true, pattern: /^\d+\.\d+\.\d+$/ }]}><Input /></Form.Item>
          <Form.Item name="items" label="항목 자산 ID (L2 이상 파생본, 줄바꿈·쉼표로 구분)" rules={[{ required: true }]}><Input.TextArea rows={3} /></Form.Item>
          <Form.Item name="card" label="데이터 카드 (기준 문서 18.2장 항목, 빈 필수 항목이 있으면 게시 불가)"><Input.TextArea rows={12} style={{ fontFamily: 'monospace' }} /></Form.Item>
          <Button type="primary" htmlType="submit" loading={addVersion.isPending}>만들기</Button>
        </Form>
      </Modal>
    </Space>
  )
}

// 데이터셋 버전 계보 (T06-b): 항목 → 원본 사슬·비식별 모델 버전·검수자
function LineageModal({ target, onClose }: { target: { ds: string; vid: string } | null; onClose: () => void }) {
  const q = useQuery({
    queryKey: ['lineage', target?.vid], enabled: !!target,
    queryFn: () => api<LineageT>(`/v1/datasets/${target?.ds}/versions/${target?.vid}/lineage`),
  })
  return (
    <Modal open={!!target} width={960} title={`계보 — ${q.data?.version ?? ''}`} onCancel={onClose} footer={null} destroyOnClose>
      {q.error && <Alert type="error" showIcon message={q.error instanceof ApiError ? explain(q.error.code).cause : String(q.error)} />}
      {!!q.data?.hidden_item_count && <Alert type="info" showIcon style={{ marginBottom: 8 }} message={`다른 기관 항목 ${q.data.hidden_item_count}개는 보이지 않습니다`} />}
      <Table rowKey="asset_id" size="small" loading={q.isLoading} pagination={false} dataSource={q.data?.items ?? []}
        columns={[
          { title: '파일', dataIndex: 'path' },
          { title: '원본', key: 'src', render: (_, i) => (
            <span>{i.chain.map((c) => `${c.relation}←${c.variant}`).join(' · ') || '-'}<br /><Link to={`/assets/${i.source_asset_id}`}>{i.source_asset_id}</Link></span>) },
          { title: '처리 모델', key: 'm', render: (_, i) => (i.processing
            ? <span>{i.processing.model.name} {i.processing.model.version}<br /><Typography.Text type="secondary">{i.processing.model.sha256 ? i.processing.model.sha256.slice(0, 12) : 'sha256 없음(모의)'} · {kst(i.processing.processed_at)}</Typography.Text></span>
            : '-') },
          { title: '검수', key: 'r', render: (_, i) => i.reviews.map((r) => <Tag key={r.review_id}>{r.status} · {r.reviewer_id}{r.recheck ? ' · 재검수' : ''}</Tag>) },
        ]} />
      <Typography.Text type="secondary">매니페스트 sha256 {q.data?.manifest_sha256}</Typography.Text>
    </Modal>
  )
}

// 이용기관: 카탈로그·신청·권한·다운로드
export function Catalog() {
  const qc = useQueryClient()
  const fail = useFail()
  const sets = useQuery({ queryKey: ['datasets'], queryFn: () => api<{ items: DatasetT[] }>('/v1/datasets') })
  const reqs = useQuery({ queryKey: ['access-requests'], queryFn: () => api<{ items: AccessReq[] }>('/v1/access-requests') })
  const grants = useQuery({ queryKey: ['grants'], queryFn: () => api<{ items: GrantT[] }>('/v1/grants') })
  const [target, setTarget] = useState<{ ds: DatasetT; vid: string } | null>(null)
  const [files, setFiles] = useState<{ path: string; url: string; byte_size: number }[] | null>(null)
  const refresh = () => ['access-requests', 'grants'].forEach((k) => void qc.invalidateQueries({ queryKey: [k] }))
  const request = useMutation({
    mutationFn: (v: { purpose: string; purpose_detail: string; start: string; end: string; security_measures: string }) => post('/v1/access-requests', {
      dataset_version_id: target?.vid, purpose: v.purpose, purpose_detail: v.purpose_detail, security_measures: v.security_measures,
      period: { start_date: v.start, end_date: v.end }, agreement_accepted: true,
    }),
    onSuccess: () => { setTarget(null); refresh() }, onError: fail,
  })
  const download = useMutation({
    mutationFn: (grant_id: string) => post<{ files: { path: string; url: string; byte_size: number }[] }>('/v1/distributions', { grant_id }),
    onSuccess: (r) => setFiles(r.files), onError: fail,
  })
  const confirm = useMutation({ mutationFn: (id: string) => post(`/v1/grants/${id}/destruction-confirmation`, {}), onSuccess: refresh, onError: fail })
  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card title="데이터셋 카탈로그">
        <Table rowKey="dataset_id" size="small" loading={sets.isLoading} dataSource={sets.data?.items ?? []} locale={{ emptyText: '이용할 수 있는 데이터셋이 없습니다' }}
          expandable={{ expandedRowRender: (d) => <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{JSON.stringify(d.data_card, null, 2)}</pre> }}
          columns={[
            { title: '이름', dataIndex: 'title' },
            { title: '버전', key: 'v', render: (_, d) => d.versions.map((v) => (
              <Space key={v.dataset_version_id}><Tag>{v.version} · {v.item_count}개 · {bytes(v.total_bytes)}</Tag>
                <Button size="small" onClick={() => setTarget({ ds: d, vid: v.dataset_version_id })}>이용 신청</Button></Space>)) },
          ]} />
      </Card>
      <Card title="내 신청">
        <Table rowKey="access_request_id" size="small" dataSource={reqs.data?.items ?? []}
          columns={[{ title: '신청', dataIndex: 'access_request_id' }, { title: '목적', dataIndex: 'purpose' },
            { title: '상태', dataIndex: 'status', render: (s: string) => <Tag>{s}</Tag> }, { title: '응답 기한', dataIndex: 'deadline_at', render: kst }]} />
      </Card>
      <Card title="이용 권한">
        <Table rowKey="grant_id" size="small" dataSource={grants.data?.items ?? []}
          columns={[{ title: '권한', dataIndex: 'grant_id' }, { title: '상태', dataIndex: 'status', render: (s: string) => <Tag>{s}</Tag> },
            { title: '만료', dataIndex: 'expires_at', render: kst },
            { title: '', key: 'a', render: (_, g) => g.status === 'GRANTED' ? <Button size="small" type="primary" onClick={() => download.mutate(g.grant_id)}>다운로드 링크 받기</Button>
              : !g.destruction_confirmed_at ? <Space direction="vertical"><Typography.Text type="secondary">{g.notice}</Typography.Text><Button size="small" onClick={() => confirm.mutate(g.grant_id)}>파기 확인 제출</Button></Space> : '파기 확인됨' }]} />
      </Card>
      <Modal open={!!target} title={`이용 신청 — ${target?.ds.title ?? ''}`} onCancel={() => setTarget(null)} footer={null} destroyOnClose>
        <Form layout="vertical" onFinish={request.mutate}>
          <Form.Item name="purpose" label="목적" rules={[{ required: true }]}><Select virtual={false} options={PURPOSES.map((p) => ({ value: p, label: p }))} /></Form.Item>
          <Form.Item name="purpose_detail" label="목적 상세 (10자 이상)" rules={[{ required: true, min: 10 }]}><Input.TextArea rows={2} /></Form.Item>
          <Space><Form.Item name="start" label="시작일" rules={[{ required: true }]}><Input type="date" /></Form.Item>
            <Form.Item name="end" label="종료일" rules={[{ required: true }]}><Input type="date" /></Form.Item></Space>
          <Form.Item name="security_measures" label="보안 조치 (보관 위치·접근 통제·파기 방법)" rules={[{ required: true, min: 10 }]}><Input.TextArea rows={2} /></Form.Item>
          <Alert type="info" showIcon message="신청하면 이용 약정에 동의한 것으로 봅니다. 모든 제공기관과 운영기관이 승인해야 하고, 응답 기한이 지나면 거부됩니다." style={{ marginBottom: 12 }} />
          <Button type="primary" htmlType="submit" loading={request.isPending}>약정 동의하고 신청</Button>
        </Form>
      </Modal>
      <Modal open={!!files} title="다운로드 (링크는 몇 분 뒤 만료)" onCancel={() => setFiles(null)} footer={null}>
        {files?.map((f) => <div key={f.path}><a href={f.url} target="_blank" rel="noreferrer">{f.path}</a> ({bytes(f.byte_size)})</div>)}
      </Modal>
    </Space>
  )
}

// 제공기관: 이용 신청 승인·거절, 권한 철회, 이용 이력
export function ProviderAccess({ me }: { me: Me }) {
  const qc = useQueryClient()
  const fail = useFail()
  const isAdmin = me.roles.includes('ORG_ADMIN')
  const reqs = useQuery({ queryKey: ['access-requests'], queryFn: () => api<{ items: AccessReq[] }>('/v1/access-requests') })
  const grants = useQuery({ queryKey: ['grants'], queryFn: () => api<{ items: GrantT[] }>('/v1/grants') })
  const history = useQuery({ queryKey: ['usage-history'], queryFn: () => api<{ items: { kind: string; at: string; consumer_organization?: string; purpose?: string; detail?: string }[] }>('/v1/usage-history') })
  const refresh = () => ['access-requests', 'grants', 'usage-history'].forEach((k) => void qc.invalidateQueries({ queryKey: [k] }))
  const decide = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: string }) => post(`/v1/access-requests/${id}/decisions`, { decision, reason: decision === 'APPROVE' ? '제공기관 승인' : '제공기관 거절' }),
    onSuccess: refresh, onError: fail,
  })
  const revoke = useMutation({ mutationFn: (id: string) => post(`/v1/grants/${id}/revoke`, { reason: '제공기관 철회' }), onSuccess: refresh, onError: fail })
  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card title="이용 신청 (우리 자산이 든 데이터셋)">
        <Table rowKey="access_request_id" size="small" dataSource={reqs.data?.items ?? []} locale={{ emptyText: '신청이 없습니다' }}
          columns={[{ title: '신청', dataIndex: 'access_request_id' }, { title: '목적', dataIndex: 'purpose' },
            { title: '상태', dataIndex: 'status', render: (s: string) => <Tag>{s}</Tag> }, { title: '기한', dataIndex: 'deadline_at', render: kst },
            { title: '', key: 'a', render: (_, r) => isAdmin && r.status === 'REQUESTED' && (
              <Space><Button size="small" type="primary" onClick={() => decide.mutate({ id: r.access_request_id, decision: 'APPROVE' })}>승인</Button>
                <Button size="small" danger onClick={() => decide.mutate({ id: r.access_request_id, decision: 'DENY' })}>거절</Button></Space>) }]} />
        <Typography.Text type="secondary">한 기관이라도 거절하면 신청은 끝나며 운영자가 뒤집을 수 없습니다.</Typography.Text>
      </Card>
      <Card title="부여된 이용 권한">
        <Table rowKey="grant_id" size="small" dataSource={grants.data?.items ?? []}
          columns={[{ title: '권한', dataIndex: 'grant_id' }, { title: '상태', dataIndex: 'status', render: (s: string) => <Tag>{s}</Tag> },
            { title: '만료', dataIndex: 'expires_at', render: kst },
            { title: '', key: 'a', render: (_, g) => isAdmin && g.status === 'GRANTED' && (
              <Button size="small" danger onClick={() => Modal.confirm({ title: '이용 권한 철회', content: '즉시 새 다운로드 링크 발급이 멈춥니다. 이미 다운로드된 파일은 회수되지 않습니다.', onOk: () => revoke.mutate(g.grant_id) })}>철회</Button>) }]} />
      </Card>
      <Card title="이용 이력">
        <Table rowKey={(r) => `${r.at}-${r.kind}-${r.detail ?? ''}`} size="small" dataSource={history.data?.items ?? []}
          columns={[{ title: '시각', dataIndex: 'at', render: kst }, { title: '구분', dataIndex: 'kind' },
            { title: '이용기관', dataIndex: 'consumer_organization' }, { title: '목적', dataIndex: 'purpose' }, { title: '내용', dataIndex: 'detail' }]} />
      </Card>
    </Space>
  )
}

