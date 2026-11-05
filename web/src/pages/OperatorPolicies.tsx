// 운영 콘솔 정책 편집 (09번 요구사항 10.1·10.2, ADR-0038): 기관 쿼터·악성코드 검사 요구, 보존 정책, 작업 우선순위.
// 바꾸기는 다중 인증·사유 필수이고, 서버가 바뀐 항목의 전후 값과 사유를 감사 로그에 남긴다.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, App, Button, Card, Form, Input, InputNumber, Modal, Select, Switch, Table, Tabs, Tag, Typography } from 'antd'
import { useState } from 'react'
import type { components } from '../contract/api.gen'
import { api, ApiError, patch } from '../lib/api'
import { bytes, kst } from '../lib/format'
import { explain } from '../lib/problems'

type OrgPolicy = components['schemas']['OrganizationPolicy']
type Retention = components['schemas']['RetentionPolicy']
type JobPolicy = components['schemas']['JobPolicy']
const GIB = 1024 ** 3

function errorText(e: unknown): string {
  if (!(e instanceof ApiError)) return String(e)
  const fields = e.problem.errors?.map((x) => x.message).join(' / ')
  return [explain(e.code).cause, e.problem.detail, fields].filter(Boolean).join(' — ')
}

// 정책 한 줄을 바꾸는 대화상자: 바뀐 항목만 보내고 사유는 필수
function EditDialog<T extends object>({ title, open, onClose, path, initial, children, toBody, invalidate }: {
  title: string; open: boolean; onClose: () => void; path: string; initial: Record<string, unknown>
  children: React.ReactNode; toBody: (v: Record<string, unknown>) => Record<string, unknown>; invalidate: string
}) {
  const { message } = App.useApp()
  const qc = useQueryClient()
  const [error, setError] = useState<string | null>(null)
  const save = useMutation({
    mutationFn: (body: Record<string, unknown>) => patch<T>(path, body),
    onSuccess: () => { void message.success('바꿨습니다 (감사 로그에 전후 값이 남습니다)'); onClose(); void qc.invalidateQueries({ queryKey: [invalidate] }) },
    onError: (e) => setError(errorText(e)),
  })
  const submit = (v: Record<string, unknown>) => {
    const body = toBody(v)
    const changed = Object.fromEntries(Object.entries(body).filter(([k, x]) => x !== initial[k]))
    if (!Object.keys(changed).length) { onClose(); return }
    save.mutate({ ...changed, reason: v.reason })
  }
  return (
    <Modal open={open} title={title} onCancel={onClose} footer={null} destroyOnClose>
      <Typography.Paragraph type="secondary">다중 인증으로 로그인한 운영자만 바꿀 수 있습니다.</Typography.Paragraph>
      {error && <Alert style={{ marginBottom: 12 }} type="error" showIcon message={error} closable onClose={() => setError(null)} />}
      <Form layout="vertical" onFinish={submit} initialValues={initial}>
        {children}
        <Form.Item name="reason" label="사유 (감사 로그에 남음)" rules={[{ required: true, whitespace: true, min: 2, max: 500 }]}>
          <Input.TextArea rows={2} />
        </Form.Item>
        <Button type="primary" htmlType="submit" loading={save.isPending}>저장</Button>
      </Form>
    </Modal>
  )
}

function OrgPolicies() {
  const list = useQuery({ queryKey: ['org-policies'], queryFn: () => api<{ items: OrgPolicy[] }>('/v1/organization-policies') })
  const [edit, setEdit] = useState<OrgPolicy | null>(null)
  return (
    <>
      <Table rowKey="organization_id" size="small" pagination={false} loading={list.isLoading} dataSource={list.data?.items ?? []}
        columns={[{ title: '기관', key: 'n', render: (_, o) => <>{o.name} <Typography.Text type="secondary">({o.org_type})</Typography.Text></> },
          { title: '쿼터', dataIndex: 'quota_bytes', render: bytes },
          { title: '사용량', key: 'u', render: (_, o) => <>{bytes(o.usage_bytes)} {o.over_quota && <Tag color="red">쿼터 초과</Tag>}</> },
          { title: '악성코드 검사', key: 'm', render: (_, o) => (o.malware_scan_required ? <Tag color="blue">요구</Tag> : '안 함') },
          { title: '변경', key: 'e', render: (_, o) => <Button size="small" onClick={() => setEdit(o)}>변경</Button> }]} />
      {edit && (
        <EditDialog<OrgPolicy> title={`기관 정책 — ${edit.name}`} open onClose={() => setEdit(null)} invalidate="org-policies"
          path={`/v1/organization-policies/${edit.organization_id}`}
          initial={{ quota_bytes: edit.quota_bytes, malware_scan_required: edit.malware_scan_required, quota_gib: edit.quota_bytes / GIB }}
          toBody={(v) => ({ quota_bytes: Math.round(Number(v.quota_gib) * GIB), malware_scan_required: v.malware_scan_required })}>
          <Form.Item name="quota_gib" label="쿼터 (GiB) — 사용량보다 낮추면 새 업로드만 막히고 데이터는 지우지 않습니다" rules={[{ required: true }]}>
            <InputNumber min={0} step={1} style={{ width: 200 }} aria-label="쿼터 (GiB)" />
          </Form.Item>
          <Form.Item name="malware_scan_required" label="악성코드 검사 요구" valuePropName="checked"><Switch /></Form.Item>
        </EditDialog>
      )}
    </>
  )
}

function RetentionPolicies() {
  const list = useQuery({ queryKey: ['retention-policies'], queryFn: () => api<{ items: Retention[] }>('/v1/retention-policies') })
  const [edit, setEdit] = useState<Retention | null>(null)
  return (
    <>
      <Typography.Paragraph type="secondary">새로 등록·파생되는 자산부터 적용합니다. 이미 정한 보존 기한은 다시 계산하지 않습니다.</Typography.Paragraph>
      <Table rowKey="target" size="small" pagination={false} loading={list.isLoading} dataSource={list.data?.items ?? []}
        columns={[{ title: '대상', dataIndex: 'target' },
          { title: '보존', key: 'd', render: (_, r) => (r.retention_days ? `${r.retention_days}일` : '무기한') },
          { title: '만료 동작', key: 'a', render: (_, r) => <>{r.expiry_action === 'DELETE' ? '삭제' : '보관'} {r.action_locked && <Tag>고정</Tag>}</> },
          { title: '알림', key: 'n', render: (_, r) => `${r.notify_days_before}일 전` },
          { title: '바꾼 시각', dataIndex: 'updated_at', render: kst },
          { title: '변경', key: 'e', render: (_, r) => <Button size="small" onClick={() => setEdit(r)}>변경</Button> }]} />
      {edit && (
        <EditDialog<Retention> title={`보존 정책 — ${edit.target}`} open onClose={() => setEdit(null)} invalidate="retention-policies"
          path={`/v1/retention-policies/${encodeURIComponent(edit.target)}`}
          initial={{ retention_days: edit.retention_days, expiry_action: edit.expiry_action, notify_days_before: edit.notify_days_before }}
          toBody={(v) => ({ retention_days: v.retention_days ?? null, expiry_action: v.expiry_action, notify_days_before: v.notify_days_before })}>
          {edit.action_locked && <Alert style={{ marginBottom: 12 }} type="info" showIcon message="이 대상의 만료 동작은 바꿀 수 없습니다 (domain.md 9장)" />}
          <Form.Item name="expiry_action" label="만료 동작">
            <Select virtual={false} disabled={edit.action_locked} aria-label="만료 동작" options={[{ value: 'DELETE', label: '삭제' }, { value: 'KEEP', label: '보관' }]} />
          </Form.Item>
          <Form.Item name="retention_days" label="보존 일수 (삭제일 때 필수)"><InputNumber min={1} max={36500} style={{ width: 200 }} aria-label="보존 일수" /></Form.Item>
          <Form.Item name="notify_days_before" label="만료 전 알림 (일)" rules={[{ required: true }]}><InputNumber min={0} max={365} style={{ width: 200 }} aria-label="만료 전 알림 일수" /></Form.Item>
        </EditDialog>
      )}
    </>
  )
}

function JobPolicies() {
  const list = useQuery({ queryKey: ['job-policies'], queryFn: () => api<{ items: JobPolicy[] }>('/v1/job-policies') })
  const [edit, setEdit] = useState<JobPolicy | null>(null)
  return (
    <>
      <Typography.Paragraph type="secondary">우선순위는 대기 중인 작업에도 바로 적용되고, 최대 시도는 새로 등록하는 작업부터 적용됩니다.</Typography.Paragraph>
      <Table rowKey="job_type" size="small" pagination={false} loading={list.isLoading} dataSource={list.data?.items ?? []}
        columns={[{ title: '작업', dataIndex: 'job_type' }, { title: '우선순위', dataIndex: 'priority' },
          { title: '자원', key: 'r', render: (_, j) => `${j.resource_class}${j.resource_class === 'GPU' ? ` (없으면 ${j.gpu_fallback === 'CPU' ? 'CPU' : '대기'})` : ''}` },
          { title: '최대 시도', dataIndex: 'max_attempts' }, { title: '대기', dataIndex: 'queued_jobs' },
          { title: '변경', key: 'e', render: (_, j) => <Button size="small" onClick={() => setEdit(j)}>변경</Button> }]} />
      {edit && (
        <EditDialog<JobPolicy> title={`작업 정책 — ${edit.job_type}`} open onClose={() => setEdit(null)} invalidate="job-policies"
          path={`/v1/job-policies/${encodeURIComponent(edit.job_type)}`}
          initial={{ priority: edit.priority, max_attempts: edit.max_attempts }}
          toBody={(v) => ({ priority: v.priority, max_attempts: v.max_attempts })}>
          <Form.Item name="priority" label="우선순위 (0~1000, 클수록 먼저)" rules={[{ required: true }]}><InputNumber min={0} max={1000} style={{ width: 200 }} aria-label="우선순위" /></Form.Item>
          <Form.Item name="max_attempts" label="최대 시도 (1~20)" rules={[{ required: true }]}><InputNumber min={1} max={20} style={{ width: 200 }} aria-label="최대 시도" /></Form.Item>
        </EditDialog>
      )}
    </>
  )
}

export function PolicyEditor() {
  return (
    <Card title="정책 편집 (쿼터·보존·작업 우선순위)">
      <Tabs items={[{ key: 'org', label: '기관', children: <OrgPolicies /> },
        { key: 'retention', label: '보존 정책', children: <RetentionPolicies /> },
        { key: 'jobs', label: '작업 우선순위', children: <JobPolicies /> }]} />
    </Card>
  )
}
