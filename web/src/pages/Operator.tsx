// 운영 콘솔 (09번 요구사항 10): 기관 사용량, 격리 목록, 감사 로그, 보존 dry-run. 정책 편집 화면은 다음 단계.
import { useMutation, useQuery } from '@tanstack/react-query'
import { App, Button, Card, Progress, Space, Table, Tag, Typography } from 'antd'
import { api, ApiError, post, type Asset, type OrgUsage, type Page } from '../lib/api'
import { bytes, kst, QUARANTINE_LABEL } from '../lib/format'
import { explain } from '../lib/problems'

interface Audit { audit_id: string; occurred_at: string; actor_kind: string; actor_id: string | null; action: string; target_id: string | null; result: string }
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
            { title: '쿼터', dataIndex: 'quota_bytes', render: bytes }]} />
      </Card>
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
      <Card title="감사 로그 (최근)">
        <Table rowKey="audit_id" size="small" dataSource={audit.data?.items ?? []}
          columns={[{ title: '시각', dataIndex: 'occurred_at', render: kst }, { title: '행위', dataIndex: 'action' },
            { title: '주체', key: 'a', render: (_, r) => `${r.actor_kind} ${r.actor_id ?? ''}` }, { title: '대상', dataIndex: 'target_id' },
            { title: '결과', dataIndex: 'result', render: (x: string) => <Tag color={x === 'SUCCESS' ? 'green' : 'red'}>{x}</Tag> }]} />
      </Card>
    </Space>
  )
}
