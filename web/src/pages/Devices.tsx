// 장비 (09번 요구사항 4): 목록·등록(ORG_ADMIN)·등록 코드 발급·자격증명 폐기, 정책 표시, 유일본 경고, 설치 안내.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, App, Button, Card, Form, Input, Modal, Select, Space, Table, Tag, Typography } from 'antd'
import { useState } from 'react'
import { api, ApiError, post, type Device, type Me, type Page } from '../lib/api'
import { bytes, kst } from '../lib/format'
import { explain } from '../lib/problems'

const MODE_LABEL: Record<string, string> = {
  C0_METADATA_ONLY: 'C0 메타데이터만', C1_SAMPLE_PREVIEW: 'C1 샘플·미리보기', C2_EVENT_CLIPS: 'C2 이벤트 클립',
  C3_APPROVED_FULL: 'C3 원본 전체',
}

interface BackupEstimate { asset_count: number; total_bytes: number; applied: boolean; usage: { quota_bytes: number; used_total_bytes: number; after_bytes: number } }

function errorText(e: unknown): string {
  if (e instanceof ApiError) {
    const h = explain(e.code)
    return `${h.cause}. ${h.action}`
  }
  return String(e)
}

export function Devices({ me }: { me: Me }) {
  const { message } = App.useApp()
  const qc = useQueryClient()
  const isAdmin = me.roles.includes('ORG_ADMIN')
  const devices = useQuery({ queryKey: ['devices'], queryFn: () => api<Page<Device>>('/v1/devices?limit=500') })
  const [code, setCode] = useState<{ device: Device; code: string; expires_at: string } | null>(null)
  const [creating, setCreating] = useState(false)
  const issue = useMutation({
    mutationFn: (d: Device) => post<{ code: string; expires_at: string }>(`/v1/devices/${d.device_id}/enrollment-codes`, {})
      .then((r) => ({ device: d, ...r })),
    onSuccess: setCode,
    onError: (e) => message.error(errorText(e)),
  })
  const revoke = useMutation({
    mutationFn: (d: Device) => post(`/v1/devices/${d.device_id}/credentials/revoke`, {}),
    onSuccess: () => { message.success('자격증명을 폐기했습니다. 에이전트를 다시 등록해야 합니다'); void qc.invalidateQueries({ queryKey: ['devices'] }) },
    onError: (e) => message.error(errorText(e)),
  })
  const create = useMutation({
    mutationFn: (v: Record<string, unknown>) => post<Device>('/v1/devices', v),
    onSuccess: () => { setCreating(false); void qc.invalidateQueries({ queryKey: ['devices'] }) },
    onError: (e) => message.error(errorText(e)),
  })
  const [backupFor, setBackupFor] = useState<{ device: Device; est: BackupEstimate } | null>(null)
  const estimate = useMutation({
    mutationFn: (d: Device) => post<BackupEstimate>('/v1/backup-requests', { device_id: d.device_id, dry_run: true }).then((est) => ({ device: d, est })),
    onSuccess: setBackupFor, onError: (e) => message.error(errorText(e)),
  })
  const applyBackup = useMutation({
    mutationFn: (d: Device) => post<BackupEstimate>('/v1/backup-requests', { device_id: d.device_id, dry_run: false }),
    onSuccess: (r) => { setBackupFor(null); message.success(`${r.asset_count}개 자산(${bytes(r.total_bytes)})을 백업 대상으로 올렸습니다`) },
    onError: (e) => message.error(errorText(e)),
  })
  const items = devices.data?.items ?? []
  const collectors = items.filter((d) => d.kind === 'COLLECTOR')
  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card title="장비" extra={isAdmin && <Button type="primary" onClick={() => setCreating(true)}>장비 등록</Button>}>
        <Table rowKey="device_id" dataSource={items} loading={devices.isLoading} pagination={false}
          columns={[
            { title: '이름', dataIndex: 'name' },
            { title: '구분', dataIndex: 'kind', render: (k: string) => (k === 'COLLECTOR' ? '수집기' : '원천 장비') },
            { title: '유형', dataIndex: 'device_type' },
            { title: '수집 방식', dataIndex: 'collection_mode', render: (m: string) => MODE_LABEL[m] ?? m },
            { title: '현장 사본', key: 'copy', render: (_, d) => (
              <Space direction="vertical" size={0}>
                <span>{d.local_copy_policy}</span>
                {d.center_copy_may_be_only_copy && d.kind === 'SOURCE' && (
                  <Tag color="orange">센터 원본이 유일본일 수 있음</Tag>
                )}
              </Space>
            ) },
            { title: '상태', dataIndex: 'status' },
            { title: '마지막 하트비트', dataIndex: 'last_heartbeat_at', render: kst },
            { title: '', key: 'act', render: (_, d) => isAdmin && (d.kind === 'COLLECTOR' ? (
              <Space>
                <Button size="small" onClick={() => issue.mutate(d)} loading={issue.isPending}>등록 코드</Button>
                <Button size="small" danger onClick={() => revoke.mutate(d)}>자격증명 폐기</Button>
              </Space>
            ) : d.center_copy_may_be_only_copy && (
              <Button size="small" onClick={() => estimate.mutate(d)} loading={estimate.isPending}>원본 백업 신청</Button>
            )) },
          ]} />
        {items.some((d) => d.center_copy_may_be_only_copy && d.kind === 'SOURCE') && (
          <Alert style={{ marginTop: 16 }} type="warning" showIcon
            message="현장 사본이 순환 삭제되거나 확인되지 않은 장비가 있습니다"
            description="센터에 올라온 원본이 유일본일 수 있습니다. 중요한 자료는 '원본 백업 신청'으로 독립 백업 대상에 올리세요 (백업 용량은 기관 사용량에 들어갑니다)." />
        )}
      </Card>
      <Card title="에이전트 설치 안내">
        <Typography.Paragraph>
          현장 PC에 <Typography.Text code>marine-agent</Typography.Text>를 내려받아 관리자 권한으로 아래 명령을 실행합니다.
          등록 코드는 1회용이고 24시간 동안 유효합니다.
        </Typography.Paragraph>
        <Typography.Paragraph>
          <pre>{`marine-agent enroll --server ${window.location.origin} --code <등록코드> --watch-root <감시 폴더>
marine-agent install
marine-agent start
marine-agent status`}</pre>
        </Typography.Paragraph>
        <Typography.Text type="secondary">에이전트는 감시 폴더의 원본을 지우거나 옮기지 않습니다.</Typography.Text>
      </Card>
      <Modal open={!!code} title="등록 코드" onCancel={() => setCode(null)} footer={null}>
        {code && (
          <Space direction="vertical">
            <Typography.Text>{code.device.name}</Typography.Text>
            <Typography.Text code copyable style={{ fontSize: 18 }}>{code.code}</Typography.Text>
            <Typography.Text type="secondary">만료 {kst(code.expires_at)} · 이 화면을 닫으면 다시 볼 수 없습니다</Typography.Text>
          </Space>
        )}
      </Modal>
      <Modal open={!!backupFor} title={`원본 백업 신청 — ${backupFor?.device.name ?? ''}`} onCancel={() => setBackupFor(null)}
        okText="신청" okButtonProps={{ disabled: !backupFor?.est.asset_count || (backupFor?.est.usage.after_bytes ?? 0) > (backupFor?.est.usage.quota_bytes ?? 0) }}
        onOk={() => backupFor && applyBackup.mutate(backupFor.device)} confirmLoading={applyBackup.isPending}>
        {backupFor && (
          <Space direction="vertical">
            <Typography.Text>대상: 아직 백업되지 않은 원본 {backupFor.est.asset_count}개 · {bytes(backupFor.est.total_bytes)}</Typography.Text>
            <Typography.Text>기관 사용량: {bytes(backupFor.est.usage.used_total_bytes)} → {bytes(backupFor.est.usage.after_bytes)} (쿼터 {bytes(backupFor.est.usage.quota_bytes)})</Typography.Text>
            {backupFor.est.usage.after_bytes > backupFor.est.usage.quota_bytes && <Alert type="error" showIcon message="쿼터를 넘어 신청할 수 없습니다. 운영기관에 쿼터 조정을 요청하세요." />}
            <Typography.Text type="secondary">다음 정기 백업부터 별도 저장소에 복사됩니다.</Typography.Text>
          </Space>
        )}
      </Modal>
      <Modal open={creating} title="장비 등록" onCancel={() => setCreating(false)} footer={null} destroyOnClose>
        <Form layout="vertical" onFinish={(v) => create.mutate(Object.fromEntries(Object.entries(v).filter(([, x]) => x)))}>
          <Form.Item name="name" label="이름" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="kind" label="구분" rules={[{ required: true }]}>
            <Select virtual={false} options={[{ value: 'SOURCE', label: '원천 장비 (카메라·센서 등)' }, { value: 'COLLECTOR', label: '수집기 (에이전트 PC)' }]} />
          </Form.Item>
          <Form.Item name="device_type" label="유형" rules={[{ required: true }]}>
            <Select virtual={false} options={['CAMERA', 'NVR', 'SENSOR', 'AGENT_PC', 'EDGE_BOX', 'SERVER', 'OTHER'].map((v) => ({ value: v, label: v }))} />
          </Form.Item>
          <Form.Item name="collector_device_id" label="연결할 수집기 (원천 장비)">
            <Select virtual={false} allowClear options={collectors.map((d) => ({ value: d.device_id, label: d.name }))} />
          </Form.Item>
          <Form.Item name="local_copy_policy" label="현장 사본 정책" rules={[{ required: true }]}
            extra="연계 카드에서 확인한 값. 순환 녹화면 ROTATING">
            <Select virtual={false} options={['RETAINED', 'ROTATING', 'NONE', 'UNKNOWN'].map((v) => ({ value: v, label: v }))} />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={create.isPending}>등록</Button>
        </Form>
      </Modal>
    </Space>
  )
}
