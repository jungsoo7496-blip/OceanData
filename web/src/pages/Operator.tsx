// 운영 콘솔 (09번 요구사항 10): 기관 사용량, 정책 편집(OperatorPolicies.tsx), 격리 목록, 감사 로그, 보존 dry-run, 약정(03번), 긴급 열람(02번), 백업 회차(08번).
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { App, Button, Card, DatePicker, Form, Input, InputNumber, Modal, Progress, Select, Space, Table, Tag, Typography } from 'antd'
import { useState } from 'react'
import type { components } from '../contract/api.gen'
import { api, ApiError, post, type Asset, type OrgUsage, type Page } from '../lib/api'
import { bytes, kst, QUARANTINE_LABEL } from '../lib/format'
import { explain } from '../lib/problems'
import { PolicyEditor } from './OperatorPolicies'

interface Audit { audit_id: string; occurred_at: string; actor_kind: string; actor_id: string | null; action: string; target_id: string | null; result: string }
// 계약에서 생성한 타입 (손으로 쓴 타입 대신, 09번 작업 16)
type Agreement = components['schemas']['Agreement']
type Grant = components['schemas']['BreakGlassGrant']
type BackupRun = components['schemas']['BackupRun']
type Forecast = components['schemas']['CapacityForecast']
type ModelEntry = components['schemas']['ModelEntry']
type ModelCreate = components['schemas']['ModelCreate']
const PURPOSES = ['JOINT_RESEARCH', 'AI_TRAINING', 'MODEL_IMPROVEMENT', 'COMMERCIAL_SERVICE', 'EXTERNAL_SALE']

function failText(e: unknown): string {
  return e instanceof ApiError ? `${explain(e.code).cause} ${e.problem.detail ?? ''}` : String(e)
}

// 약정 기록 (03번 요구사항 1.4): 운영자·다중 인증. 종료는 유효기간 끝을 당긴다
function Agreements() {
  const { message, modal } = App.useApp()
  const qc = useQueryClient()
  const list = useQuery({ queryKey: ['agreements'], queryFn: () => api<{ items: Agreement[] }>('/v1/agreements') })
  const [adding, setAdding] = useState(false)
  const refresh = () => void qc.invalidateQueries({ queryKey: ['agreements'] })
  const add = useMutation({
    mutationFn: (v: { organization_id: string; tier: string; reference: string; valid_from: { toISOString: () => string }; valid_until?: { toISOString: () => string }; allowed_purposes?: string[] }) =>
      post('/v1/agreements', { organization_id: v.organization_id, tier: v.tier, reference: v.reference, valid_from: v.valid_from.toISOString(),
        ...(v.valid_until ? { valid_until: v.valid_until.toISOString() } : {}), allowed_purposes: v.tier === 'TIER2' ? v.allowed_purposes ?? [] : [] }),
    onSuccess: () => { setAdding(false); refresh() }, onError: (e) => message.error(failText(e)),
  })
  const end = useMutation({
    mutationFn: (a: Agreement) => post(`/v1/agreements/${a.agreement_id}/end`, { reason: '운영자 종료' }),
    onSuccess: refresh, onError: (e) => message.error(failText(e)),
  })
  return (
    <Card title="기관 약정" extra={<Button onClick={() => setAdding(true)}>약정 기록</Button>}>
      <Table rowKey="agreement_id" size="small" dataSource={list.data?.items ?? []}
        columns={[{ title: '기관', dataIndex: 'organization_id' }, { title: '구분', dataIndex: 'tier' }, { title: '참조', dataIndex: 'reference' },
          { title: '허용 목적', dataIndex: 'allowed_purposes', render: (x: string[]) => x.map((p) => <Tag key={p}>{p}</Tag>) },
          { title: '기간', key: 'v', render: (_, a) => `${kst(a.valid_from)} ~ ${a.valid_until ? kst(a.valid_until) : ''}` },
          { title: '', key: 'e', render: (_, a) => (a.in_effect
            ? <Button size="small" danger onClick={() => modal.confirm({ title: '약정 종료', content: '지금부터 무효가 됩니다. 이미 공유된 자산·이용 권한은 하향·철회 절차로 따로 처리하세요.', onOk: () => end.mutate(a) })}>종료</Button>
            : <Tag>종료</Tag>) }]} />
      <Modal open={adding} title="약정 기록 (다중 인증 필요)" onCancel={() => setAdding(false)} footer={null} destroyOnClose>
        <Form layout="vertical" onFinish={add.mutate} initialValues={{ tier: 'TIER2' }}>
          <Form.Item name="organization_id" label="기관 ID" rules={[{ required: true, pattern: /^org_[0-9A-HJKMNP-TV-Z]{26}$/ }]}><Input /></Form.Item>
          <Form.Item name="tier" label="구분" rules={[{ required: true }]}><Select options={[{ value: 'TIER1', label: '1단 수집·관리' }, { value: 'TIER2', label: '2단 공동활용 제공' }]} /></Form.Item>
          <Form.Item name="reference" label="약정서 참조 (문서 번호 등, 원본은 올리지 않음)" rules={[{ required: true, min: 2 }]}><Input /></Form.Item>
          <Space>
            <Form.Item name="valid_from" label="시작" rules={[{ required: true }]}><DatePicker showTime /></Form.Item>
            <Form.Item name="valid_until" label="끝 (비우면 무기한)"><DatePicker showTime /></Form.Item>
          </Space>
          <Form.Item name="allowed_purposes" label="허용 목적 (2단만)"><Select mode="multiple" options={PURPOSES.map((p) => ({ value: p, label: p }))} /></Form.Item>
          <Button type="primary" htmlType="submit" loading={add.isPending}>기록</Button>
        </Form>
      </Modal>
    </Card>
  )
}

// 긴급 열람 (02번 요구사항 8): 요청 → 다른 운영자 승인 → 요청자만 원본 접근, 기간 끝나면 회수
function BreakGlass() {
  const { message } = App.useApp()
  const qc = useQueryClient()
  const list = useQuery({ queryKey: ['break-glass'], queryFn: () => api<{ items: Grant[] }>('/v1/break-glass-grants') })
  const [asking, setAsking] = useState(false)
  const refresh = () => void qc.invalidateQueries({ queryKey: ['break-glass'] })
  const ask = useMutation({
    mutationFn: (v: { target_type: string; target_id: string; reason: string; hours: number }) =>
      post('/v1/break-glass-grants', { target_type: v.target_type, target_id: v.target_id, reason: v.reason, duration_s: Math.round(v.hours * 3600) }),
    onSuccess: () => { setAsking(false); refresh(); message.info('다른 운영자의 승인이 필요합니다') }, onError: (e) => message.error(failText(e)),
  })
  const act = useMutation({
    mutationFn: ({ g, kind }: { g: Grant; kind: 'APPROVE' | 'DENY' | 'REVOKE' }) => (kind === 'REVOKE'
      ? post(`/v1/break-glass-grants/${g.grant_id}/revoke`, { reason: '작업 종료' })
      : post(`/v1/break-glass-grants/${g.grant_id}/decision`, { decision: kind })),
    onSuccess: refresh, onError: (e) => message.error(failText(e)),
  })
  return (
    <Card title="긴급 열람" extra={<Button onClick={() => setAsking(true)}>요청</Button>}>
      <Table rowKey="grant_id" size="small" dataSource={list.data?.items ?? []}
        columns={[{ title: '기관', dataIndex: 'organization_id' }, { title: '대상', key: 't', render: (_, g) => `${g.target_type} ${g.target_id}` },
          { title: '사유', dataIndex: 'reason' }, { title: '요청자', dataIndex: 'requested_by' },
          { title: '상태', dataIndex: 'status', render: (s: string) => <Tag color={s === 'ACTIVE' ? 'orange' : s === 'REQUESTED' ? 'blue' : undefined}>{s}</Tag> },
          { title: '만료', dataIndex: 'expires_at', render: kst },
          { title: '', key: 'a', render: (_, g) => (
            <Space>
              {g.status === 'REQUESTED' && <><Button size="small" type="primary" onClick={() => act.mutate({ g, kind: 'APPROVE' })}>승인</Button>
                <Button size="small" onClick={() => act.mutate({ g, kind: 'DENY' })}>거절</Button></>}
              {(g.status === 'ACTIVE' || g.status === 'REQUESTED') && <Button size="small" danger onClick={() => act.mutate({ g, kind: 'REVOKE' })}>종료</Button>}
            </Space>) }]} />
      <Typography.Text type="secondary">요청한 운영자 본인은 승인할 수 없습니다. 승인되면 대상 기관 관리자에게 통지되고, 원본 접근마다 감사 로그에 남습니다.</Typography.Text>
      <Modal open={asking} title="긴급 열람 요청 (다중 인증 필요)" onCancel={() => setAsking(false)} footer={null} destroyOnClose>
        <Form layout="vertical" onFinish={ask.mutate} initialValues={{ target_type: 'ASSET', hours: 1 }}>
          <Form.Item name="target_type" label="대상 종류"><Select options={[{ value: 'ASSET', label: '자산' }, { value: 'SESSION', label: '실증회차 (그 회차 자산 전부)' }]} /></Form.Item>
          <Form.Item name="target_id" label="대상 ID" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="reason" label="사유 (장애 번호 등 구체적으로)" rules={[{ required: true, min: 10 }]}><Input.TextArea rows={3} /></Form.Item>
          <Form.Item name="hours" label="기간 (시간, 최대 24)" rules={[{ required: true }]}><InputNumber min={0.05} max={24} step={0.5} /></Form.Item>
          <Button type="primary" htmlType="submit" loading={ask.isPending}>요청</Button>
        </Form>
      </Modal>
    </Card>
  )
}

// 용량 예측 (08번 요구사항 8): 일별 스냅숏 추세로 설정 사용률 도달 예상일
function CapacityForecast() {
  const f = useQuery({ queryKey: ['capacity'], queryFn: () => api<Forecast>('/v1/capacity-forecast') })
  const d = f.data
  return (
    <Card title="용량 예측" loading={f.isLoading}>
      {d && (
        <Space direction="vertical">
          <Typography.Text>현재 {bytes(d.current_bytes)}{d.capacity_bytes ? ` / 전체 ${bytes(d.capacity_bytes)} (${Math.round((d.current_ratio ?? 0) * 1000) / 10}%)` : ''}</Typography.Text>
          <Typography.Text>추세 {d.slope_bytes_per_day != null ? `${bytes(Math.max(0, d.slope_bytes_per_day))}/일` : '-'} (최근 {d.window_days}일 스냅숏 {d.history.length}개)</Typography.Text>
          {d.reach_date
            ? <Tag color={d.within_lead_time ? 'red' : 'green'}>사용률 {Math.round(d.threshold_ratio * 100)}% 도달 예상 {d.reach_date}{d.within_lead_time ? ` · 리드타임 ${d.lead_time_days}일 안` : ''}</Tag>
            : <Typography.Text type="secondary">{d.note}</Typography.Text>}
        </Space>
      )}
    </Card>
  )
}

// Model_Registry (05번 요구사항 8): 이름·버전·sha256·허용 라이선스가 기록된 모델만 로드한다
function Models() {
  const { message } = App.useApp()
  const qc = useQueryClient()
  const list = useQuery({ queryKey: ['models'], queryFn: () => api<{ items: ModelEntry[]; allowed_licenses: string[] }>('/v1/models') })
  const [adding, setAdding] = useState(false)
  const add = useMutation({
    mutationFn: (v: ModelCreate) => post<ModelEntry>('/v1/models', v),
    onSuccess: () => { setAdding(false); void qc.invalidateQueries({ queryKey: ['models'] }) },
    onError: (e) => message.error(failText(e)),
  })
  return (
    <Card title="모델 등록부 (Model_Registry)" extra={<Button onClick={() => setAdding(true)}>모델 등록</Button>}>
      <Table rowKey={(m) => `${m.name}@${m.version}`} size="small" dataSource={list.data?.items ?? []} locale={{ emptyText: '등록된 모델이 없습니다 (지금은 모의 탐지기)' }}
        columns={[{ title: '이름', dataIndex: 'name' }, { title: '버전', dataIndex: 'version' }, { title: '라이선스', dataIndex: 'license' },
          { title: 'sha256', dataIndex: 'sha256', render: (x: string) => <Typography.Text code>{x.slice(0, 12)}…</Typography.Text> },
          { title: '로드', dataIndex: 'loadable', render: (x: boolean) => (x ? <Tag color="green">가능</Tag> : <Tag color="red">불가</Tag>) },
          { title: '등록', dataIndex: 'created_at', render: kst }]} />
      <Modal open={adding} title="모델 등록 (다중 인증 필요)" onCancel={() => setAdding(false)} footer={null} destroyOnClose>
        <Form layout="vertical" onFinish={add.mutate}>
          <Space wrap>
            <Form.Item name="name" label="이름" rules={[{ required: true, pattern: /^[a-z0-9][a-z0-9._-]{0,119}$/ }]}><Input /></Form.Item>
            <Form.Item name="version" label="버전" rules={[{ required: true }]}><Input /></Form.Item>
            <Form.Item name="license" label="라이선스 (SPDX)" rules={[{ required: true }]}>
              <Select style={{ width: 180 }} options={(list.data?.allowed_licenses ?? []).map((l) => ({ value: l, label: l }))} />
            </Form.Item>
          </Space>
          <Form.Item name="sha256" label="가중치 sha256" rules={[{ required: true, pattern: /^[0-9a-f]{64}$/ }]}><Input /></Form.Item>
          <Form.Item name="weights_uri" label="가중치 위치 (파일 이름 또는 저장소 참조)" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="training_data_terms" label="학습 데이터 이용 조건 검토 결과" rules={[{ required: true, min: 2 }]}><Input.TextArea rows={3} /></Form.Item>
          <Button type="primary" htmlType="submit" loading={add.isPending}>등록</Button>
        </Form>
      </Modal>
    </Card>
  )
}

// 백업 회차·복원 시험 결과 (08번 요구사항 6·7)
function BackupRuns() {
  const { message } = App.useApp()
  const qc = useQueryClient()
  const runs = useQuery({ queryKey: ['backup-runs'], queryFn: () => api<{ items: BackupRun[] }>('/v1/backup-runs') })
  const start = useMutation({
    mutationFn: () => post('/v1/backup-runs', {}),
    onSuccess: () => { message.success('백업 작업을 등록했습니다 (워커가 실행)'); void qc.invalidateQueries({ queryKey: ['backup-runs'] }) },
    onError: (e) => message.error(failText(e)),
  })
  return (
    <Card title="백업 회차" extra={<Button onClick={() => start.mutate()} loading={start.isPending}>지금 백업</Button>}>
      <Table rowKey="backup_run_id" size="small" dataSource={runs.data?.items ?? []} locale={{ emptyText: '백업 기록이 없습니다' }}
        columns={[{ title: '시작', dataIndex: 'started_at', render: kst },
          { title: '상태', dataIndex: 'status', render: (s: string) => <Tag color={s === 'SUCCEEDED' ? 'green' : s === 'FAILED' ? 'red' : 'blue'}>{s}</Tag> },
          { title: '대상', key: 'o', render: (_, r) => `${r.object_count}개 · ${bytes(r.total_bytes)} (새로 ${r.copied_count})` },
          { title: '복원 시험', key: 'rt', render: (_, r) => (r.restore_test ? <Tag color={r.restore_test.ok ? 'green' : 'red'}>{r.restore_test.ok ? '통과' : '실패'} {kst(r.restore_test.tested_at)}</Tag> : '-') },
          { title: '오류', dataIndex: 'error' }]} />
      <Typography.Text type="secondary">복원 시험은 서버에서 make restore-test로 실행합니다 (임시 DB·위치에 복원해 행 수·해시 대조).</Typography.Text>
    </Card>
  )
}

interface DryRun { total_count: number; total_bytes: number; excluded_legal_hold: number; targets: { target: string; count: number; bytes: number }[] }

export function OperatorConsole() {
  const { message } = App.useApp()
  const usage = useQuery({ queryKey: ['usage', 'all'], queryFn: () => api<{ items: OrgUsage[] }>('/v1/usage') })
  const quarantine = useQuery({ queryKey: ['assets', 'q-all'], queryFn: () => api<Page<Asset>>('/v1/assets?ingest_status=QUARANTINED&limit=200') })
  const audit = useQuery({ queryKey: ['audit'], queryFn: () => api<Page<Audit>>('/v1/audit-logs?limit=100') })
  const dry = useMutation({
    mutationFn: () => post<DryRun>('/v1/retention/dry-run', {}),
    onError: (e) => message.error(e instanceof ApiError ? explain(e.code).cause : String(e)),
  })
  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card title="기관 사용량">
        <Table rowKey="organization_id" size="small" pagination={false} dataSource={usage.data?.items ?? []}
          columns={[{ title: '기관', dataIndex: 'organization_id' },
            { title: '사용률', key: 'r', render: (_, u) => <Progress percent={Math.round(u.usage_ratio * 1000) / 10} size="small" style={{ width: 160 }} /> },
            { title: '등록', key: 's', render: (_, u) => bytes(u.breakdown.stored_bytes) },
            { title: '격리', key: 'q', render: (_, u) => bytes(u.breakdown.quarantine_bytes) },
            { title: '업로드 예약', key: 'res', render: (_, u) => bytes(u.breakdown.reserved_bytes) },
            { title: '신청 백업', key: 'bk', render: (_, u) => bytes(u.breakdown.backup_bytes ?? 0) },
            { title: '쿼터', dataIndex: 'quota_bytes', render: bytes }]} />
      </Card>
      <PolicyEditor />
      <Card title="보존 만료 dry-run" extra={<Button onClick={() => dry.mutate()} loading={dry.isPending}>미리 보기</Button>}>
        {dry.data ? (
          <>
            <Typography.Paragraph>삭제 대상 {dry.data.total_count}건 ({bytes(dry.data.total_bytes)}), 법적 보류로 제외 {dry.data.excluded_legal_hold}건</Typography.Paragraph>
            <Table rowKey="target" size="small" pagination={false} dataSource={dry.data.targets}
              columns={[{ title: '대상', dataIndex: 'target' }, { title: '건수', dataIndex: 'count' }, { title: '용량', dataIndex: 'bytes', render: bytes }]} />
            <Typography.Text type="secondary">실제 삭제는 운영자가 확인한 뒤 서버 명령(retention --execute)으로 실행합니다.</Typography.Text>
          </>
        ) : <Typography.Text type="secondary">삭제하지 않고 대상만 계산합니다.</Typography.Text>}
      </Card>
      <Card title="격리 목록 (전 기관)">
        <Table rowKey="asset_id" size="small" dataSource={quarantine.data?.items ?? []}
          columns={[{ title: '기관', key: 'o', render: (_, a) => (a as Asset & { organization_id?: string }).organization_id },
            { title: '사유', key: 'r', render: (_, a) => <Tag color="red">{QUARANTINE_LABEL[a.quarantine_reason ?? '']}</Tag> },
            { title: '받은 시각', dataIndex: 'ingested_at', render: kst }]} />
      </Card>
      <CapacityForecast />
      <Agreements />
      <BreakGlass />
      <BackupRuns />
      <Models />
      <Card title="감사 로그 (최근)">
        <Table rowKey="audit_id" size="small" dataSource={audit.data?.items ?? []}
          columns={[{ title: '시각', dataIndex: 'occurred_at', render: kst }, { title: '행위', dataIndex: 'action' },
            { title: '주체', key: 'a', render: (_, r) => `${r.actor_kind} ${r.actor_id ?? ''}` }, { title: '대상', dataIndex: 'target_id' },
            { title: '결과', dataIndex: 'result', render: (x: string) => <Tag color={x === 'SUCCESS' ? 'green' : 'red'}>{x}</Tag> }]} />
      </Card>
    </Space>
  )
}
