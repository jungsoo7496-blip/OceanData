// 비식별 검수 (14번 요구사항 4): 배정된 대상만, 표본 프레임 원본 대비, 통과·미탐(수동 가림)·반려. 품질 집계(운영자).
// 표기는 "처리 이력" — 자동 처리만으로 적정성을 보증하지 않는다 (요구사항 5.4).
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, App, Button, Card, Col, Form, Input, InputNumber, Modal, Row, Select, Space, Table, Tag, Typography } from 'antd'
import { useEffect, useState } from 'react'
import { api, ApiError, post, type Me } from '../lib/api'
import { currentUser } from '../lib/auth'
import { kst } from '../lib/format'
import { explain } from '../lib/problems'

interface ReviewT {
  review_id: string; original_asset_id: string; deid_asset_id: string; risk_level: string; samples: number[]; recheck: boolean
  status: string; created_at: string; processing: { model?: { name: string; version: string }; params?: Record<string, unknown>; detections?: Record<string, number>; note?: string }
}
const CLASSES = ['FACE', 'LICENSE_PLATE', 'VESSEL_ID_TEXT', 'SCREEN_TEXT']

function Frame({ reviewId, t, which }: { reviewId: string; t: number; which: 'ORIGINAL' | 'DEID' }) {
  const [src, setSrc] = useState<string>()
  useEffect(() => {
    let url: string | undefined
    void (async () => {
      const user = await currentUser()
      const r = await fetch(`/v1/deid-reviews/${reviewId}/frame?t=${t}&which=${which}`, { headers: { Authorization: `Bearer ${user?.access_token ?? ''}` } })
      if (r.ok) { url = URL.createObjectURL(await r.blob()); setSrc(url) }
    })()
    return () => { if (url) URL.revokeObjectURL(url) }
  }, [reviewId, t, which])
  return src ? <img src={src} alt={`${which === 'ORIGINAL' ? '원본' : '비식별본'} ${t}초`} style={{ width: '100%' }} /> : <Typography.Text type="secondary">불러오는 중</Typography.Text>
}

export function ReviewQueue({ me }: { me: Me }) {
  const { message } = App.useApp()
  const qc = useQueryClient()
  const isOp = me.roles.includes('PLATFORM_OPERATOR')
  const list = useQuery({ queryKey: ['deid-reviews'], queryFn: () => api<{ items: ReviewT[] }>('/v1/deid-reviews?status=ASSIGNED') })
  const quality = useQuery({ queryKey: ['deid-quality'], queryFn: () => api<{ reviews: number; reprocess_ratio: number; classes: { class: string; detected: number; missed: number; miss_rate: number }[]; note: string }>('/v1/deid-quality'), enabled: isOp })
  const [open, setOpen] = useState<ReviewT | null>(null)
  const [idx, setIdx] = useState(0)
  const [missedOpen, setMissedOpen] = useState(false)
  const decide = useMutation({
    mutationFn: (body: Record<string, unknown>) => post<{ recheck_review_ids: string[] }>(`/v1/deid-reviews/${open?.review_id}/decision`, body),
    onSuccess: (r) => {
      message.success(r.recheck_review_ids?.length ? `기록했습니다. 같은 회차 ${r.recheck_review_ids.length}건을 재검수로 표시했습니다` : '기록했습니다')
      setOpen(null); setMissedOpen(false)
      void qc.invalidateQueries({ queryKey: ['deid-reviews'] }); void qc.invalidateQueries({ queryKey: ['deid-quality'] })
    },
    onError: (e) => message.error(e instanceof ApiError ? explain(e.code).cause : String(e)),
  })
  const t = open?.samples[idx] ?? 0
  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card title="배정된 비식별 검수">
        <Table rowKey="review_id" size="small" loading={list.isLoading} dataSource={list.data?.items ?? []} locale={{ emptyText: '배정된 검수가 없습니다' }}
          columns={[
            { title: '비식별본', dataIndex: 'deid_asset_id' },
            { title: '위험도', dataIndex: 'risk_level', render: (r: string) => <Tag color={r === 'HIGH' ? 'red' : r === 'MEDIUM' ? 'orange' : 'default'}>{r}</Tag> },
            { title: '표본', key: 's', render: (_, r) => `${r.samples.length}개` },
            { title: '탐지(처리 이력)', key: 'd', render: (_, r) => Object.entries(r.processing.detections ?? {}).map(([k, v]) => `${k} ${v}`).join(', ') || '-' },
            { title: '', key: 'r', render: (_, r) => r.recheck && <Tag color="purple">재검수</Tag> },
            { title: '배정', dataIndex: 'created_at', render: kst },
            { title: '', key: 'a', render: (_, r) => <Button size="small" type="primary" onClick={() => { setOpen(r); setIdx(0) }}>검수</Button> },
          ]} />
      </Card>
      {isOp && quality.data && (
        <Card title="비식별 품질 (검수 결과 집계)">
          <Typography.Paragraph>검수 {quality.data.reviews}건 · 재처리 비율 {(quality.data.reprocess_ratio * 100).toFixed(1)}%</Typography.Paragraph>
          <Table rowKey="class" size="small" pagination={false} dataSource={quality.data.classes}
            columns={[{ title: '클래스', dataIndex: 'class' }, { title: '탐지', dataIndex: 'detected' }, { title: '미탐', dataIndex: 'missed' },
              { title: '미탐률', dataIndex: 'miss_rate', render: (v: number) => `${(v * 100).toFixed(1)}%` }]} />
          <Typography.Text type="secondary">{quality.data.note}</Typography.Text>
        </Card>
      )}
      <Modal open={!!open} width={1000} title={open && `검수 — 표본 ${idx + 1}/${open.samples.length} (${t}초)`} onCancel={() => setOpen(null)}
        footer={open && [
          <Button key="p" disabled={idx === 0} onClick={() => setIdx(idx - 1)}>이전 표본</Button>,
          <Button key="n" disabled={idx >= open.samples.length - 1} onClick={() => setIdx(idx + 1)}>다음 표본</Button>,
          <Button key="r" danger onClick={() => decide.mutate({ result: 'REJECT', note: '재처리 필요' })}>반려(재처리)</Button>,
          <Button key="m" onClick={() => setMissedOpen(true)}>미탐 표시</Button>,
          <Button key="ok" type="primary" disabled={idx < open.samples.length - 1} onClick={() => decide.mutate({ result: 'PASS' })}>통과 (표본 모두 확인)</Button>,
        ]}>
        {open && (
          <>
            <Alert type="info" showIcon style={{ marginBottom: 12 }} message={`처리 이력: ${open.processing.model?.name ?? ''}@${open.processing.model?.version ?? ''} · ${JSON.stringify(open.processing.params ?? {})}`} description={open.processing.note} />
            <Row gutter={12}>
              <Col span={12}><Typography.Text strong>원본</Typography.Text><Frame reviewId={open.review_id} t={t} which="ORIGINAL" /></Col>
              <Col span={12}><Typography.Text strong>비식별본</Typography.Text><Frame reviewId={open.review_id} t={t} which="DEID" /></Col>
            </Row>
          </>
        )}
      </Modal>
      <Modal open={missedOpen} title="미탐 표시 (같은 회차 비식별 결과가 재검수 대상이 됩니다)" onCancel={() => setMissedOpen(false)} footer={null} destroyOnClose>
        <Form layout="vertical" initialValues={{ cls: 'FACE', t, t0: Math.max(0, t - 0.5), t1: t + 0.5 }}
          onFinish={(v: { cls: string; t: number; t0: number; t1: number; x?: number; y?: number; w?: number; h?: number; note?: string }) => decide.mutate({
            result: 'MISSED', missed: [{ cls: v.cls, t: v.t }], note: v.note,
            manual_regions: v.w && v.h ? [{ t0: v.t0, t1: v.t1, x: v.x ?? 0, y: v.y ?? 0, w: v.w, h: v.h }] : [],
          })}>
          <Space wrap>
            <Form.Item name="cls" label="클래스"><Select virtual={false} style={{ width: 180 }} options={CLASSES.map((c) => ({ value: c, label: c }))} /></Form.Item>
            <Form.Item name="t" label="시각(초)"><InputNumber min={0} step={0.1} /></Form.Item>
          </Space>
          <Typography.Text strong>수동 가림 영역 (선택 — 넣으면 그 구간을 다시 렌더링)</Typography.Text>
          <Space wrap>
            {(['t0', 't1', 'x', 'y', 'w', 'h'] as const).map((k) => <Form.Item key={k} name={k} label={k}><InputNumber min={0} /></Form.Item>)}
          </Space>
          <Form.Item name="note" label="메모"><Input.TextArea rows={2} /></Form.Item>
          <Button type="primary" htmlType="submit" loading={decide.isPending}>기록</Button>
        </Form>
      </Modal>
    </Space>
  )
}

// 자산 화면의 장면 검색 (요구사항 7.2)
export function SceneSearch() {
  const [q, setQ] = useState('')
  const [res, setRes] = useState<{ query_tags: string[]; items: { asset_id: string; t_offset: number; score: number }[] } | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const run = async () => {
    setErr(null)
    try { setRes(await api(`/v1/scenes/search?q=${encodeURIComponent(q)}`)) } catch (e) { setRes(null); setErr(e instanceof ApiError ? (e.problem.errors?.[0]?.message ?? explain(e.code).cause) : String(e)) }
  }
  return (
    <Card title="장면 검색 (예: 야간 부유물, 소형 선박)" size="small">
      <Space.Compact style={{ width: '100%', maxWidth: 520 }}>
        <Input value={q} onChange={(e) => setQ(e.target.value)} onPressEnter={() => void run()} placeholder="찾을 장면을 문장으로" aria-label="장면 검색어" />
        <Button type="primary" onClick={() => void run()}>찾기</Button>
      </Space.Compact>
      {err && <Alert type="warning" style={{ marginTop: 8 }} message={err} />}
      {res && (
        <Table style={{ marginTop: 8 }} size="small" rowKey={(r) => `${r.asset_id}:${r.t_offset}`} pagination={{ pageSize: 10 }} dataSource={res.items}
          title={() => `해석: ${res.query_tags.join(', ')} (태그 어휘 기준선)`}
          columns={[{ title: '자산', dataIndex: 'asset_id', render: (id: string) => <a href={`/assets/${id}`}>{id}</a> },
            { title: '시각(초)', dataIndex: 't_offset' }, { title: '유사도', dataIndex: 'score' }]} />
      )}
    </Card>
  )
}
