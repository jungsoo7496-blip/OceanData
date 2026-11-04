// 운영 콘솔 — 수집 모니터링 (11번 요구사항 2, 5, 6): 기관·장비별 수집 지연, 실패 원인 상위, 장애, 감시 규칙, 정기 점검.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { App, Badge, Button, Card, Modal, Select, Space, Table, Tag, Typography } from 'antd'
import { useMemo, useState } from 'react'
import { IncidentTable } from '../components/Incidents'
import { api, ApiError, patch, post, type AgentHealth, type IngestStatus, type Page } from '../lib/api'
import { duration, kst, RULE_LABEL, SEVERITY_COLOR } from '../lib/format'
import { explain } from '../lib/problems'

interface WatchRule {
  rule_code: string; description: string; enabled: boolean; severity: string; params: Record<string, unknown>
  notify_org: boolean; notify_operator: boolean; channels: string[]; realert_interval_s: number; data_source_ready: boolean
}
interface InspectionItem { item_code: string; title: string; auto: boolean; result: string | null; note: string | null }
interface Inspection {
  inspection_id: string; cycle: string; period_key: string; status: string; assignee: string | null; due_at: string
  items: InspectionItem[]
}

const CYCLE_LABEL: Record<string, string> = { DAILY: '일', WEEKLY: '주', MONTHLY: '월', QUARTERLY: '분기', YEARLY: '연' }
const RESULT_LABEL: Record<string, string> = { PASS: '정상', FAIL: '이상', NOT_APPLICABLE: '해당 없음', AUTO_UNAVAILABLE: '자동 확인 불가' }

// 어제(KST) — 일일 점검은 지난 하루를 본다
function yesterdayKst(): string {
  const d = new Date(Date.now() + 9 * 3600_000 - 86400_000)
  return d.toISOString().slice(0, 10)
}

export function Monitoring() {
  const { message } = App.useApp()
  const qc = useQueryClient()
  const fail = (e: unknown) => message.error(e instanceof ApiError ? `${explain(e.code).cause} ${e.problem.detail ?? ''}` : String(e))
  const status = useQuery({ queryKey: ['ingest-status'], queryFn: () => api<IngestStatus>('/v1/ingest-status'), refetchInterval: 60_000 })
  const rules = useQuery({ queryKey: ['watch-rules'], queryFn: () => api<{ items: WatchRule[] }>('/v1/watch-rules') })
  const insp = useQuery({ queryKey: ['inspections'], queryFn: () => api<Page<Inspection>>('/v1/inspections?limit=500') })
  const [org, setOrg] = useState<string | undefined>()
  const [open, setOpen] = useState<Inspection | null>(null)
  const [results, setResults] = useState<Record<string, string>>({})
  const devices = useMemo(() => (status.data?.devices ?? [])
    .filter((d) => !org || d.organization_id === org)
    .sort((a, b) => (b.lag_s ?? -1) - (a.lag_s ?? -1)), [status.data, org])
  const orgs = [...new Set((status.data?.devices ?? []).map((d) => d.organization_id ?? ''))].filter(Boolean)
  const start = useMutation({
    mutationFn: () => post<Inspection>('/v1/inspections', { cycle: 'DAILY', period_key: yesterdayKst() }),
    onSuccess: (i) => { void qc.invalidateQueries({ queryKey: ['inspections'] }); setOpen(i); setResults({}) },
    onError: fail,
  })
  const save = useMutation({
    mutationFn: ({ i, complete }: { i: Inspection; complete: boolean }) => patch<Inspection>(`/v1/inspections/${i.inspection_id}`, {
      items: Object.entries(results).map(([item_code, result]) => ({ item_code, result })), ...(complete ? { complete: true } : {}),
    }),
    onSuccess: (i) => { setOpen(i.status === 'COMPLETED' ? null : i); void qc.invalidateQueries({ queryKey: ['inspections'] }) },
    onError: fail,
  })
  const items = (status.data?.open_incidents ?? []).filter((i) => !org || i.organization_id === org)
  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card title="수집 지연 (지연이 큰 순)" extra={
        <Select virtual={false} allowClear placeholder="기관" aria-label="기관 선택" style={{ minWidth: 220 }} value={org}
          options={orgs.map((o) => ({ value: o, label: o }))} onChange={(v: string | undefined) => setOrg(v)} />}>
        <Table<AgentHealth> rowKey="device_id" size="small" loading={status.isLoading} dataSource={devices} pagination={{ pageSize: 20 }}
          columns={[
            { title: '기관', dataIndex: 'organization_id' },
            { title: '장비', key: 'n', render: (_, h) => `${h.name ?? h.device_id} (${h.kind === 'COLLECTOR' ? '수집기' : '원천'})` },
            { title: '상태', dataIndex: 'status', render: (s: string) => <Badge status={s === 'OK' ? 'success' : s === 'NEVER_SEEN' ? 'default' : s === 'STALE' ? 'warning' : 'error'} text={s} /> },
            { title: '지연', dataIndex: 'lag_s', render: duration },
            { title: '하트비트', dataIndex: 'heartbeat_lag_s', render: (s: number | null) => (s == null ? '-' : `${duration(s)} 전`) },
            { title: '기대 주기', dataIndex: 'expected_interval_s', render: duration },
            { title: '대기', dataIndex: 'queue_items' },
            { title: '시계 오차', dataIndex: 'clock_offset_ms', render: (ms: number | null) => (ms == null ? '-' : `${(ms / 1000).toFixed(1)}초`) },
            { title: '정책', dataIndex: 'policy_mismatch', render: (m: boolean) => (m ? <Tag color="orange">버전 다름</Tag> : '-') },
            { title: '최근 오류', dataIndex: 'last_error_code', render: (c: string | null) => c ?? '-' },
          ]} />
      </Card>
      <Card title="실패 원인 상위 (최근 24시간)">
        <Table rowKey={(r) => `${r.source}:${r.code}`} size="small" pagination={false} dataSource={status.data?.top_failure_reasons ?? []}
          locale={{ emptyText: '최근 실패가 없습니다' }}
          columns={[{ title: '구분', dataIndex: 'source' }, { title: '코드', dataIndex: 'code' }, { title: '건수', dataIndex: 'count' }]} />
      </Card>
      <Card title={`열린 장애 ${items.length}건`}>
        <IncidentTable items={items} canEdit showOrg loading={status.isLoading} />
      </Card>
      <Card title="감시 규칙" extra={<Typography.Text type="secondary">조정은 다중 인증 후 API(PATCH /v1/watch-rules)로</Typography.Text>}>
        <Table rowKey="rule_code" size="small" pagination={false} dataSource={rules.data?.items ?? []}
          columns={[
            { title: '규칙', key: 'r', render: (_, r) => RULE_LABEL[r.rule_code] ?? r.rule_code },
            { title: '사용', key: 'e', render: (_, r) => (!r.data_source_ready ? <Tag>신호원 대기</Tag> : r.enabled ? <Tag color="green">켜짐</Tag> : <Tag>꺼짐</Tag>) },
            { title: '심각도', dataIndex: 'severity', render: (s: string) => <Tag color={SEVERITY_COLOR[s]}>{s}</Tag> },
            { title: '임계값', dataIndex: 'params', render: (p: Record<string, unknown>) => <Typography.Text code>{JSON.stringify(p)}</Typography.Text> },
            { title: '대상', key: 't', render: (_, r) => [r.notify_org && '기관', r.notify_operator && '운영자'].filter(Boolean).join('·') },
            { title: '채널', dataIndex: 'channels', render: (c: string[]) => c.join(', ') },
            { title: '재알림', dataIndex: 'realert_interval_s', render: duration },
          ]} />
      </Card>
      <Card title="정기 점검" extra={<Button onClick={() => start.mutate()} loading={start.isPending}>어제 일일 점검 시작</Button>}>
        <Table rowKey="inspection_id" size="small" dataSource={[...(insp.data?.items ?? [])].reverse()} pagination={{ pageSize: 10 }}
          locale={{ emptyText: '점검 기록이 없습니다' }}
          columns={[
            { title: '주기', dataIndex: 'cycle', render: (c: string) => CYCLE_LABEL[c] ?? c },
            { title: '기간', dataIndex: 'period_key' },
            { title: '기한', dataIndex: 'due_at', render: kst },
            { title: '상태', dataIndex: 'status', render: (s: string) => <Tag color={s === 'COMPLETED' ? 'green' : 'blue'}>{s === 'COMPLETED' ? '완료' : '진행 중'}</Tag> },
            { title: '', key: 'a', render: (_, i) => i.status !== 'COMPLETED' && <Button size="small" onClick={() => { setOpen(i); setResults({}) }}>기록</Button> },
          ]} />
      </Card>
      <Modal open={!!open} width={720} title={open && `${CYCLE_LABEL[open.cycle]} 점검 ${open.period_key}`} onCancel={() => setOpen(null)}
        footer={open && [
          <Button key="s" onClick={() => save.mutate({ i: open, complete: false })} loading={save.isPending}>저장</Button>,
          <Button key="c" type="primary" onClick={() => save.mutate({ i: open, complete: true })} loading={save.isPending}>완료</Button>,
        ]}>
        {open && (
          <Table rowKey="item_code" size="small" pagination={false} dataSource={open.items}
            columns={[
              { title: '항목', dataIndex: 'title' },
              { title: '자동 결과', key: 'auto', render: (_, it) => (it.auto ? (RESULT_LABEL[it.result ?? ''] ?? '-') : '-') },
              { title: '결과', key: 'r', render: (_, it) => (
                <Select virtual={false} style={{ width: 140 }} aria-label={`${it.title} 결과`} value={results[it.item_code] ?? (it.result !== 'AUTO_UNAVAILABLE' ? it.result ?? undefined : undefined)}
                  options={['PASS', 'FAIL', 'NOT_APPLICABLE'].map((v) => ({ value: v, label: RESULT_LABEL[v] }))}
                  onChange={(v: string) => setResults((r) => ({ ...r, [it.item_code]: v }))} />
              ) },
            ]} />
        )}
      </Modal>
    </Space>
  )
}
