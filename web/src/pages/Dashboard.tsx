// Workspace 대시보드 (09번 요구사항 2, 11번 요구사항 5.2): 회차 수, 사용량·쿼터, 장비별 수집 상태, 경보·조치 안내,
// 최근 격리, 처리 대기.
import { useQuery } from '@tanstack/react-query'
import { Badge, Card, Col, List, Progress, Row, Statistic, Table, Tag, Typography } from 'antd'
import { IncidentTable } from '../components/Incidents'
import { Link } from 'react-router-dom'
import { api, type AgentHealth, type Asset, type IngestStatus, type Me, type OrgUsage, type Page, type Session } from '../lib/api'
import { bytes, duration, kst, QUARANTINE_LABEL } from '../lib/format'

const HEALTH: Record<string, { status: 'success' | 'warning' | 'error' | 'default'; text: string }> = {
  OK: { status: 'success', text: '정상' },
  STALE: { status: 'warning', text: '수집 끊김' },
  ERROR: { status: 'error', text: '오류' },
  NEVER_SEEN: { status: 'default', text: '연결 기록 없음' },
}

export function Dashboard({ me }: { me: Me }) {
  const sessions = useQuery({ queryKey: ['sessions'], queryFn: () => api<Page<Session>>('/v1/sessions?limit=500') })
  const usage = useQuery({ queryKey: ['usage'], queryFn: () => api<{ items: OrgUsage[] }>('/v1/usage') })
  const status = useQuery({ queryKey: ['ingest-status'], queryFn: () => api<IngestStatus>('/v1/ingest-status'),
    refetchInterval: 60_000 })
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
  const devices = status.data?.devices ?? []
  const incidents = status.data?.open_incidents ?? []
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
      <Col span={24}>
        <Card title={`경보 ${incidents.length}건`} extra={<Typography.Text type="secondary">행을 펼치면 조치 안내가 보입니다</Typography.Text>}>
          <IncidentTable items={incidents} canEdit={me.roles.includes('ORG_ADMIN')} loading={status.isLoading} />
        </Card>
      </Col>
      <Col xs={24} lg={14}>
        <Card title="장비별 수집 상태">
          <Table<AgentHealth> rowKey="device_id" size="small" pagination={false} dataSource={devices} loading={status.isLoading}
            columns={[
              { title: '장비', key: 'n', render: (_, h) => <span>{h.name} <Typography.Text type="secondary">{h.kind === 'COLLECTOR' ? '수집기' : '원천'}</Typography.Text></span> },
              { title: '상태', key: 'h', render: (_, h) => {
                const s = HEALTH[h.status] ?? HEALTH.NEVER_SEEN
                return <Badge status={s.status} text={s.text} />
              } },
              { title: '마지막 하트비트', dataIndex: 'last_heartbeat_at', render: kst },
              { title: '마지막 수신', key: 'reg', render: (_, h) => kst(h.last_observation_at ?? h.last_registered_at) },
              { title: '지연', dataIndex: 'lag_s', render: duration },
              { title: '대기', dataIndex: 'queue_items' },
              { title: '경보', dataIndex: 'open_alerts', render: (n: number) => (n ? <Tag color="red">{n}</Tag> : '-') },
            ]} />
          <Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>
            수집 끊김 기준은 운영기관이 감시 규칙으로 정합니다 (기본: 하트비트 주기의 3배, 최소 5분).
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
