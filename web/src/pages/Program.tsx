// 관리기관 보드 (09번 요구사항 9): 배정 사업·회차, L1 자산 메타데이터만. 원본·프록시·다운로드 URL은 없다.
import { useQuery } from '@tanstack/react-query'
import { Alert, Card, Space, Table, Tag } from 'antd'
import { api, type Asset, type Page, type Session } from '../lib/api'
import { kst, SCOPE_LABEL } from '../lib/format'
import { CaptureTime } from './Assets'

export function ProgramBoard() {
  const projects = useQuery({ queryKey: ['projects'], queryFn: () => api<Page<{ project_id: string; name: string }>>('/v1/projects') })
  const sessions = useQuery({ queryKey: ['sessions'], queryFn: () => api<Page<Session>>('/v1/sessions?limit=500') })
  const assets = useQuery({ queryKey: ['assets', 'program'], queryFn: () => api<Page<Asset>>('/v1/assets?limit=200') })
  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Alert type="info" showIcon message="관리기관 보드는 배정된 실증사업의 L1(관리기관 검토) 자료 메타데이터만 보여줍니다. 제출된 지표·보고서는 10번 보고서 기능에서 제공합니다." />
      <Card title="담당 실증사업">
        <Table rowKey="project_id" size="small" pagination={false} dataSource={projects.data?.items ?? []}
          columns={[{ title: '사업', dataIndex: 'name' }, {
            title: '회차 수', key: 'n', render: (_, p) => (sessions.data?.items ?? []).filter((s) => s.project_id === p.project_id).length }]} />
      </Card>
      <Card title="실증회차">
        <Table rowKey="session_id" size="small" dataSource={sessions.data?.items ?? []}
          columns={[{ title: '표시값', dataIndex: 'label' }, { title: '제목', dataIndex: 'title' }, { title: '시작', dataIndex: 'starts_at', render: kst }]} />
      </Card>
      <Card title="L1 제출 자산 (메타데이터)">
        <Table rowKey="asset_id" size="small" dataSource={assets.data?.items ?? []} locale={{ emptyText: 'L1로 제출된 자산이 없습니다' }}
          columns={[{ title: '유형', dataIndex: 'data_type' }, { title: '촬영시각', key: 'c', render: (_, a) => <CaptureTime a={a} /> },
            { title: '등급', key: 'l', render: (_, a) => <Tag>{SCOPE_LABEL[a.sharing_scope]}</Tag> }, { title: '품질', dataIndex: 'quality_status' }]} />
      </Card>
    </Space>
  )
}
