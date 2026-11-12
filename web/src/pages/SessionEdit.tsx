// 실증회차 수정 (09번 요구사항 5.1): 제목·기간·장소·메모·회차 장비. 표시값(label)과 실증사업은 바꾸지 않는다
// (에이전트가 폴더 이름과 표시값을 맞추고, 자산이 실증사업에 묶여 있다). 바뀐 항목만 보내고 서버가 전후 값을 감사한다.
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Alert, App, Button, DatePicker, Form, Input, Modal, Select, Typography } from 'antd'
import dayjs, { type Dayjs } from 'dayjs'
import { useState } from 'react'
import type { components } from '../contract/api.gen'
import { ApiError, patch } from '../lib/api'
import { explain } from '../lib/problems'

type SessionT = components['schemas']['Session']
type SessionPatch = components['schemas']['SessionPatch']
interface DeviceOpt { device_id: string; name: string; kind: string }
interface Values { title: string; range: [Dayjs, Dayjs | null]; site?: string; note?: string; device_ids?: string[] }

const utc = (d: Dayjs) => d.toISOString().replace(/\.\d+Z$/, 'Z')
const sorted = (xs: string[]) => [...xs].sort()

// 원래 값과 비교해 바뀐 항목만 (비운 장소·메모·종료 시각은 null)
export function sessionChanges(s: SessionT, v: Values): SessionPatch {
  const body: SessionPatch = {}
  if (v.title.trim() !== s.title) body.title = v.title.trim()
  const start = utc(v.range[0])
  if (start !== s.starts_at) body.starts_at = start
  const end = v.range[1] ? utc(v.range[1]) : null
  if (end !== (s.ends_at ?? null)) body.ends_at = end
  const site = v.site?.trim() ? v.site.trim() : null
  if (site !== (s.site ?? null)) body.site = site
  const note = v.note?.trim() ? v.note : null
  if (note !== (s.note ?? null)) body.note = note
  const devs = sorted(v.device_ids ?? [])
  if (JSON.stringify(devs) !== JSON.stringify(sorted(s.device_ids ?? []))) body.device_ids = devs
  return body
}

export function SessionEditButton({ session, devices }: { session: SessionT; devices: DeviceOpt[] }) {
  const { message } = App.useApp()
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const save = useMutation({
    mutationFn: (body: SessionPatch) => patch<SessionT>(`/v1/sessions/${session.session_id}`, body),
    onSuccess: () => {
      setOpen(false)
      void message.success('회차를 수정했습니다')
      void qc.invalidateQueries({ queryKey: ['session', session.session_id] })
      void qc.invalidateQueries({ queryKey: ['sessions'] })
    },
    onError: (e) => setError(e instanceof ApiError
      ? (e.problem.errors?.map((x) => x.message).join(' / ') || e.problem.detail || explain(e.code).cause) : String(e)),
  })
  const submit = (v: Values) => {
    const body = sessionChanges(session, v)
    if (!Object.keys(body).length) {
      setOpen(false)
      return
    }
    save.mutate(body)
  }
  return (
    <>
      <Button onClick={() => { setError(null); setOpen(true) }}>회차 수정</Button>
      <Modal open={open} title={`회차 수정 — ${session.label}`} onCancel={() => setOpen(false)} footer={null} destroyOnClose>
        <Typography.Paragraph type="secondary">
          표시값(<code>{session.label}</code>)과 실증사업은 바꿀 수 없습니다. 에이전트가 폴더 이름을 표시값과 맞춰 회차를 찾습니다.
        </Typography.Paragraph>
        {error && <Alert style={{ marginBottom: 12 }} type="error" showIcon message={error} closable onClose={() => setError(null)} />}
        <Form layout="vertical" onFinish={submit} initialValues={{
          title: session.title, site: session.site ?? '', note: session.note ?? '', device_ids: session.device_ids ?? [],
          range: [dayjs(session.starts_at), session.ends_at ? dayjs(session.ends_at) : null],
        }}>
          <Form.Item name="title" label="제목" rules={[{ required: true, whitespace: true, max: 200 }]}><Input /></Form.Item>
          <Form.Item name="range" label="기간 (KST, 종료는 비워 두면 진행 중)" rules={[{ required: true }]}>
            <DatePicker.RangePicker showTime allowEmpty={[false, true]} />
          </Form.Item>
          <Form.Item name="site" label="장소" rules={[{ max: 200 }]}><Input /></Form.Item>
          <Form.Item name="note" label="메모" rules={[{ max: 2000 }]}><Input.TextArea rows={3} /></Form.Item>
          <Form.Item name="device_ids" label="회차 장비 (실증 예정일 무수집 감시에 쓰임)">
            <Select mode="multiple" virtual={false} aria-label="회차 장비" optionFilterProp="label"
              options={devices.filter((d) => d.kind === 'SOURCE').map((d) => ({ value: d.device_id, label: d.name }))} />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={save.isPending}>저장</Button>
        </Form>
      </Modal>
    </>
  )
}
