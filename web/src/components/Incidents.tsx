// 수집 장애 표 (11번 요구사항 4, 5): 원인·조치 안내, 확인·해결·담당자·메모. 기업 화면과 운영 콘솔이 함께 쓴다.
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { App, Button, Form, Input, Modal, Space, Table, Tag, Typography } from 'antd'
import { useState } from 'react'
import { ApiError, patch, type Incident } from '../lib/api'
import { kst, RULE_LABEL, SEVERITY_COLOR } from '../lib/format'
import { explain } from '../lib/problems'

const STATUS: Record<string, string> = { OPEN: '열림', ACKNOWLEDGED: '확인함', RESOLVED: '해결' }
const nowrap = () => ({ style: { whiteSpace: 'nowrap' as const } })

export function IncidentTable({ items, canEdit, showOrg = false, loading = false }:
  { items: Incident[]; canEdit: boolean; showOrg?: boolean; loading?: boolean }) {
  const { message } = App.useApp()
  const qc = useQueryClient()
  const [editing, setEditing] = useState<Incident | null>(null)
  const update = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Record<string, string> }) => patch<Incident>(`/v1/incidents/${id}`, body),
    onSuccess: () => {
      setEditing(null)
      void qc.invalidateQueries({ queryKey: ['ingest-status'] })
      void qc.invalidateQueries({ queryKey: ['incidents'] })
    },
    onError: (e) => message.error(e instanceof ApiError ? explain(e.code).cause : String(e)),
  })
  return (
    <>
      <Table rowKey="incident_id" size="small" loading={loading} dataSource={items} pagination={{ pageSize: 20 }}
        locale={{ emptyText: '열린 경보가 없습니다' }}
        expandable={{ expandedRowRender: (i) => (
          <Space direction="vertical">
            <Typography.Text strong>조치 안내</Typography.Text>
            <Typography.Text>{i.guidance}</Typography.Text>
            {i.memo && <Typography.Text type="secondary">메모: {i.memo}</Typography.Text>}
          </Space>
        ) }}
        columns={[
          { title: '심각도', dataIndex: 'severity', onCell: nowrap, onHeaderCell: nowrap, render: (s: string) => <Tag color={SEVERITY_COLOR[s]}>{s}</Tag> },
          { title: '규칙', dataIndex: 'rule_code', onCell: nowrap, onHeaderCell: nowrap, render: (r: string) => RULE_LABEL[r] ?? r },
          ...(showOrg ? [{ title: '기관', dataIndex: 'organization_id', render: (o: string | null) => o ?? '운영' }] : []),
          { title: '내용', dataIndex: 'summary' },
          { title: '발생', dataIndex: 'opened_at', onCell: nowrap, onHeaderCell: nowrap, render: kst },
          { title: '상태', dataIndex: 'status', onCell: nowrap, onHeaderCell: nowrap, render: (s: string) => STATUS[s] ?? s },
          { title: '담당', dataIndex: 'assignee', onCell: nowrap, onHeaderCell: nowrap, render: (a: string | null) => a ?? '-' },
          ...(canEdit ? [{ title: '', key: 'act', render: (_: unknown, i: Incident) => i.status !== 'RESOLVED' && (
            <Space>
              {i.status === 'OPEN' && (
                <Button size="small" onClick={() => update.mutate({ id: i.incident_id, body: { status: 'ACKNOWLEDGED' } })}>확인</Button>
              )}
              <Button size="small" onClick={() => setEditing(i)}>처리 기록</Button>
            </Space>
          ) }] : []),
        ]} />
      <Modal open={!!editing} title="장애 처리 기록" onCancel={() => setEditing(null)} footer={null} destroyOnClose>
        {editing && (
          <Form layout="vertical" initialValues={{ assignee: editing.assignee ?? '', memo: editing.memo ?? '' }}
            onFinish={(v: { assignee: string; memo: string; resolve?: boolean }) => update.mutate({
              id: editing.incident_id,
              body: { assignee: v.assignee, memo: v.memo, ...(v.resolve ? { status: 'RESOLVED' } : {}) },
            })}>
            <Form.Item name="assignee" label="담당자" rules={[{ max: 120 }]}><Input /></Form.Item>
            <Form.Item name="memo" label="메모 (조치 내용)" rules={[{ max: 2000 }]}><Input.TextArea rows={4} /></Form.Item>
            <Space>
              <Button htmlType="submit" loading={update.isPending}>저장</Button>
              <Form.Item noStyle shouldUpdate>
                {(f) => (
                  <Button type="primary" loading={update.isPending}
                    onClick={() => update.mutate({ id: editing.incident_id, body: { ...f.getFieldsValue(), status: 'RESOLVED' } })}>
                    해결로 닫기
                  </Button>
                )}
              </Form.Item>
            </Space>
          </Form>
        )}
      </Modal>
    </>
  )
}
