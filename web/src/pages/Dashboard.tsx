// Workspace 대시보드 (09번 요구사항 2): 회차 수, 사용량·쿼터, 장비별 하트비트·마지막 수집, 최근 격리, 처리 대기.
import { useQuery, useQueries } from '@tanstack/react-query'
import { Badge, Card, Col, List, Progress, Row, Statistic, Table, Tag, Typography } from 'antd'
import { Link } from 'react-router-dom'
import { api, type AgentHealth, type Asset, type Device, type Me, type OrgUsage, type Page, type Session } from '../lib/api'
import { bytes, kst, QUARANTINE_LABEL } from '../lib/format'

const HEALTH: Record<string, { status: 'success' | 'warning' | 'error' | 'default'; text: string }> = {
  OK: { status: 'success', text: '정상' },
  STALE: { status: 'warning', text: '수집 끊김' },
  ERROR: { status: 'error', text: '오류' },
  NEVER_SEEN: { status: 'default', text: '연결 기록 없음' },
}

export function Dashboard({ me }: { me: Me }) {
  const sessions = useQuery({ queryKey: ['sessions'], queryFn: () => api<Page<Session>>('/v1/sessions?limit=500') })
  const usage = useQuery({ queryKey: ['usage'], queryFn: () => api<{ items: OrgUsage[] }>('/v1/usage') })
  const devices = useQuery({ queryKey: ['devices'], queryFn: () => api<Page<Device>>('/v1/devices?limit=500') })
  const quarantined = useQuery({
    queryKey: ['assets', 'QUARANTINED'],
    queryFn: () => api<Page<Asset>>('/v1/assets?ingest_status=QUARANTINED&limit=20'),
  })
  const pending = useQuery({
    queryKey: ['assets', 'pending'],
    queryFn: async () => {
      const lists = await Promise.all(['RECEIVED', 'VALIDATING', 'REGISTERED'].map((s) =>
        api<Page<Asset>>(`/v1/assets?ingest_status=${s}&limit=500`)))
      return lists.reduce((n, l) => n + l.items.length, 0)
    },
  })
  const collectors = (devices.data?.items ?? []).filter((d) => d.kind === 'COLLECTOR')
  const health = useQueries({
    queries: collectors.map((d) => ({
      queryKey: ['health', d.device_id],
      queryFn: () => api<AgentHealth>(`/v1/agents/${d.device_id}/health`),
    })),
  })
  const u = usage.data?.items[0]
  return (
    <Row gutter={[16, 16]}>
      <Col xs={24} md={6}><Card><Statistic title="누적 실증회차" value={sessions.data?.items.length ?? '-'} /></Card></Col>
      <Col xs={24} md={6}>
        <Card>
          <Statistic title="저장 사용량" value={bytes(u?.usage_bytes)} suffix={`/ ${bytes(u?.quota_bytes)}`} />
          <Progress percent={Math.round((u?.usage_ratio ?? 0) * 1000) / 10}
            status={(u?.usage_ratio ?? 0) >= 0.9 ? 'exception' : 'normal'} aria-label="쿼터 사용률" />
        </Card>
      </Col>
      <Col xs={24} md={6}><Card><Statistic title="처리 대기" value={pending.data ?? '-'} /></Card></Col>
      <Col xs={24} md={6}><Card><Statistic title="격리" value={quarantined.data?.items.length ?? '-'} /></Card></Col>
      <Col xs={24} lg={14}>
        <Card title="수집기 상태">
          <Table rowKey="device_id" size="small" pagination={false} dataSource={collectors}
            columns={[
              { title: '수집기', dataIndex: 'name' },
              { title: '상태', key: 'h', render: (_, _d, i) => {
                const h = health[i]?.data
                const s = HEALTH[h?.status ?? 'NEVER_SEEN']
                return <Badge status={s.status} text={s.text} />
              } },
              { title: '마지막 하트비트', key: 'hb', render: (_, _d, i) => kst(health[i]?.data?.last_heartbeat_at) },
              { title: '마지막 수집', key: 'reg', render: (_, _d, i) => kst(health[i]?.data?.last_registered_at) },
              { title: '대기', key: 'q', render: (_, _d, i) => health[i]?.data?.queue_items ?? '-' },
            ]} />
          <Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>
            수집 끊김 판정 규칙은 11번 수집 모니터링에서 정합니다.
          </Typography.Paragraph>
        </Card>
      </Col>
      <Col xs={24} lg={10}>
        <Card title="최근 격리" extra={<Link to="/assets?ingest_status=QUARANTINED">전체 보기</Link>}>
          <List size="small" dataSource={quarantined.data?.items ?? []} locale={{ emptyText: '격리된 자산이 없습니다' }}
            renderItem={(a) => (
              <List.Item>
                <Link to={`/assets/${a.asset_id}`}>{a.provenance.original_filename ?? a.asset_id}</Link>
                <Tag color="red">{QUARANTINE_LABEL[a.quarantine_reason ?? ''] ?? a.quarantine_reason}</Tag>
              </List.Item>
            )} />
        </Card>
      </Col>
      <Col span={24}>
        <Typography.Text type="secondary">기관 {me.organization_id}</Typography.Text>
      </Col>
    </Row>
  )
}
