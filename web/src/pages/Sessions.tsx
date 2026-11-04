// 실증회차 (09번 요구사항 5): 생성, 목록, 회차 미상 격리 자산을 회차에 지정.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { App, Button, Card, DatePicker, Form, Input, Modal, Select, Space, Table, Typography } from 'antd'
import { useState } from 'react'
import { api, ApiError, patch, post, type Asset, type Me, type Page, type Session } from '../lib/api'
import { kst } from '../lib/format'
import { explain } from '../lib/problems'

interface Project { project_id: string; name: string; status: string }

export function Sessions({ me }: { me: Me }) {
  const { message } = App.useApp()
  const qc = useQueryClient()
  const sessions = useQuery({ queryKey: ['sessions'], queryFn: () => api<Page<Session>>('/v1/sessions?limit=500') })
  const projects = useQuery({ queryKey: ['projects'], queryFn: () => api<Page<Project>>('/v1/projects?limit=500') })
  const unknown = useQuery({
    queryKey: ['assets', 'QUARANTINED'],
    queryFn: () => api<Page<Asset>>('/v1/assets?ingest_status=QUARANTINED&limit=500'),
    select: (p) => p.items.filter((a) => a.quarantine_reason === 'UNKNOWN_SESSION'),
  })
  const [open, setOpen] = useState(false)
  const fail = (e: unknown) => message.error(e instanceof ApiError ? explain(e.code).cause + ' — ' + (e.problem.detail ?? '') : String(e))
  const create = useMutation({
    mutationFn: (v: { project_id: string; label: string; title: string; range: [{ toISOString(): string }, { toISOString(): string } | null]; site?: string; note?: string }) =>
      post<Session>('/v1/sessions', {
        project_id: v.project_id, label: v.label, title: v.title, starts_at: v.range[0].toISOString().replace(/\.\d+Z$/, 'Z'),
        ...(v.range[1] ? { ends_at: v.range[1].toISOString().replace(/\.\d+Z$/, 'Z') } : {}),
        ...(v.site ? { site: v.site } : {}), ...(v.note ? { note: v.note } : {}),
      }),
    onSuccess: () => { setOpen(false); void qc.invalidateQueries({ queryKey: ['sessions'] }) },
    onError: fail,
  })
  const assign = useMutation({
    mutationFn: ({ asset, session }: { asset: string; session: string }) => patch<Asset>(`/v1/assets/${asset}`, { session_id: session }),
    onSuccess: () => { message.success('회차를 지정했습니다. 다시 검증한 뒤 등록됩니다'); void qc.invalidateQueries({ queryKey: ['assets'] }) },
    onError: fail,
  })
  const projectName = (id: string) => projects.data?.items.find((p) => p.project_id === id)?.name ?? id
  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card title="실증회차" extra={me.roles.some((r) => r.startsWith('ORG_')) && <Button type="primary" onClick={() => setOpen(true)}>회차 만들기</Button>}>
        <Table rowKey="session_id" dataSource={sessions.data?.items ?? []} loading={sessions.isLoading} pagination={{ pageSize: 20 }}
          columns={[
            { title: '표시값', dataIndex: 'label' },
            { title: '제목', dataIndex: 'title' },
            { title: '실증사업', dataIndex: 'project_id', render: projectName },
            { title: '시작', dataIndex: 'starts_at', render: kst },
            { title: '종료', dataIndex: 'ends_at', render: kst },
            { title: '장소', dataIndex: 'site' },
            { title: '장비 수', key: 'n', render: (_, s) => s.device_ids.length },
          ]} />
        <Typography.Text type="secondary">폴더 이름을 회차 표시값과 같게 두면 에이전트가 회차를 자동으로 찾습니다.</Typography.Text>
      </Card>
      <Card title="회차 미상 자산 (회차를 지정하면 다시 검증합니다)">
        <Table rowKey="asset_id" dataSource={unknown.data ?? []} pagination={false} locale={{ emptyText: '회차 미상 자산이 없습니다' }}
          columns={[
            { title: '파일', key: 'f', render: (_, a) => a.provenance.original_relative_path ?? a.provenance.original_filename },
            { title: '받은 시각', dataIndex: 'ingested_at', render: kst },
            { title: '회차 지정', key: 's', render: (_, a) => (
              <Select virtual={false} style={{ width: 240 }} placeholder="회차 선택" aria-label="회차 선택"
                options={(sessions.data?.items ?? []).filter((s) => s.project_id === a.project_id).map((s) => ({ value: s.session_id, label: `${s.label} ${s.title}` }))}
                onChange={(v: string) => assign.mutate({ asset: a.asset_id, session: v })} />
            ) },
          ]} />
      </Card>
      <Modal open={open} title="회차 만들기" onCancel={() => setOpen(false)} footer={null} destroyOnClose>
        <Form layout="vertical" onFinish={create.mutate}>
          <Form.Item name="project_id" label="실증사업" rules={[{ required: true }]}>
            <Select virtual={false} options={(projects.data?.items ?? []).map((p) => ({ value: p.project_id, label: p.name }))} />
          </Form.Item>
          <Form.Item name="label" label="표시값 (기관 안에서 유일, 폴더 이름)" rules={[{ required: true, max: 120 }]}><Input /></Form.Item>
          <Form.Item name="title" label="제목" rules={[{ required: true, max: 200 }]}><Input /></Form.Item>
          <Form.Item name="range" label="기간 (KST)" rules={[{ required: true }]}>
            <DatePicker.RangePicker showTime allowEmpty={[false, true]} />
          </Form.Item>
          <Form.Item name="site" label="장소"><Input /></Form.Item>
          <Form.Item name="note" label="메모"><Input.TextArea rows={3} /></Form.Item>
          <Button type="primary" htmlType="submit" loading={create.isPending}>만들기</Button>
        </Form>
      </Modal>
    </Space>
  )
}
