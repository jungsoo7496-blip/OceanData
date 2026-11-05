// 온보딩 마법사 (09번 요구사항 3.2~3.4, ADR-0037): 연계 카드 → 장비 선택 → 장비 정책 초안 → 운영자 확인 후 적용.
// - 카드에서는 구조화된 값(희망 수집 수준·현장 사본 정책·현장 보존 기간)만 미리 채운다. 나머지는 직접 넣거나 '확인 필요'.
// - 초안은 운영자가 적용하기 전까지 장비·정책을 바꾸지 않는다. 적용·반려는 운영자(다중 인증)만 한다.
// - 연계 난이도 등급은 판정 기준이 정해지지 않아 '기준 미정 — 확인 필요'로만 보인다.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, App, AutoComplete, Button, Card, Input, InputNumber, Modal, Select, Space, Steps, Table, Tag, Typography } from 'antd'
import { useState } from 'react'
import type { components } from '../contract/api.gen'
import { api, ApiError, patch, post } from '../lib/api'
import { kst } from '../lib/format'
import { explain } from '../lib/problems'

// 계약에서 생성한 타입 (09번 작업 16)
type IntegrationCard = components['schemas']['IntegrationCard']
type Draft = components['schemas']['PolicyDraft']
type FieldName = components['schemas']['PolicyDraftFieldName']
type Values = components['schemas']['PolicyDraftValues']
type Preview = components['schemas']['PolicyDraftPreview']
type Change = components['schemas']['PolicyChange']
type Device = components['schemas']['Device']
type TransferWindow = NonNullable<Values['transfer_windows']>[number]
type DraftPage = { items: Draft[]; next_cursor: string | null }
// 설정 화면의 카드 행도 받을 수 있게 필요한 부분만
export type CardLike = Pick<IntegrationCard, 'integration_card_id' | 'version' | 'open_items' | 'difficulty_grade'> & {
  content: { basic?: { project_name?: string }; equipment?: unknown }
}

const FIELDS: FieldName[] = ['collection_mode', 'allowed_data_roles', 'local_copy_policy', 'local_retention_days',
  'expected_interval_s', 'transfer_windows', 'source_timezone']
const FIELD_LABEL: Record<FieldName, string> = {
  collection_mode: '수집 수준 (collection_mode)', allowed_data_roles: '허용 data_role', local_copy_policy: '현장 사본 정책',
  local_retention_days: '현장 보존 기간 (일)', expected_interval_s: '기대 수집 주기 (초)',
  transfer_windows: '전송 허용 시간대 (수집기 전체)', source_timezone: '원천 시간대 (source_timezone)',
}
const MODES = ['C0_METADATA_ONLY', 'C1_SAMPLE_PREVIEW', 'C2_EVENT_CLIPS', 'C3_APPROVED_FULL']
const COPY = ['RETAINED', 'ROTATING', 'NONE', 'UNKNOWN']
const ROLES = [{ value: 'CORE', label: 'CORE (핵심)' }, { value: 'BYPRODUCT', label: 'BYPRODUCT (부산물)' }]
const DAYS: TransferWindow['days'] = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN']
const DAY_LABEL: Record<string, string> = { MON: '월', TUE: '화', WED: '수', THU: '목', FRI: '금', SAT: '토', SUN: '일' }
const FIELD_STATUS: Record<string, [string, string]> = {
  FROM_CARD: ['blue', '카드 값'], ENTERED: ['green', '입력함'], NEEDS_CONFIRMATION: ['orange', '확인 필요'],
}
const DRAFT_STATUS: Record<string, [string, string]> = {
  DRAFT: ['blue', '운영자 확인 전'], APPLIED: ['green', '적용됨'], REJECTED: ['red', '반려됨'],
}
const statusTag = (map: Record<string, [string, string]>, s: string) => <Tag color={map[s]?.[0]}>{map[s]?.[1] ?? s}</Tag>
// AntD 선택 상자의 자리표시 글자는 기본색 대비가 4.5:1에 못 미친다 (09번 요구사항 11.2) — 테마의 3차 글자색으로
const hint = (s: string) => <span style={{ color: 'rgba(0,0,0,0.62)' }}>{s}</span>
// 읽기 전용 표는 가로 스크롤 대신 긴 코드값(C1_SAMPLE_PREVIEW 등)을 줄바꿈한다 (좁은 화면, 스크롤 영역 초점 문제 없이)
const wrap = (s: string) => <span style={{ overflowWrap: 'anywhere' }}>{s}</span>

export function failText(e: unknown): string {
  if (!(e instanceof ApiError)) return String(e)
  const extra = [...(e.problem.errors ?? []).map((x) => `${x.pointer}: ${x.message}`),
    ...(e.problem.unmet_conditions ?? []).map((u) => `${u.condition} (${u.actual ?? '-'})`)]
  return `${explain(e.code).cause} ${e.problem.detail ?? ''} ${extra.join(', ')}`.trim()
}

const windowText = (w: TransferWindow) =>
  `${w.days.map((d) => DAY_LABEL[d]).join('·')} ${w.start_local}~${w.end_local}${w.max_kbps ? ` (최대 ${w.max_kbps} kbps)` : ''}`

function showValue(field: FieldName, v: unknown): string {
  if (field === 'transfer_windows') {
    const ws = (v ?? []) as TransferWindow[]
    return ws.length ? ws.map(windowText).join(' / ') : '제한 없음 (항상 전송)'
  }
  if (v === null || v === undefined) return '없음'
  return Array.isArray(v) ? v.join(', ') : String(v)
}

// 카드의 '확인 필요' 항목과 연계 난이도 (요구사항 3.4)
export function CardChecklist({ card }: { card: CardLike }) {
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Typography.Text strong>카드의 확인 필요 항목 (v{card.version})</Typography.Text>
      {card.open_items.length === 0 ? <Typography.Text type="secondary">없음</Typography.Text> : (
        <ul style={{ margin: 0, paddingInlineStart: 20 }}>
          {card.open_items.map((i) => (
            <li key={i.section}>{i.label}: {i.state === 'EMPTY' ? '비어 있음' : `'확인 필요' (${i.pointers.join(', ')})`}</li>
          ))}
        </ul>
      )}
      <Typography.Text>
        연계 난이도 등급: <Tag color="orange">{card.difficulty_grade.grade ?? card.difficulty_grade.note}</Tag>
      </Typography.Text>
    </Space>
  )
}

function WindowsInput({ value, onChange }: { value: TransferWindow[]; onChange: (v: TransferWindow[]) => void }) {
  const set = (i: number, w: Partial<TransferWindow>) => onChange(value.map((x, j) => (j === i ? { ...x, ...w } : x)))
  return (
    <Space direction="vertical">
      {value.length === 0 && <Typography.Text type="secondary">제한 없음 (항상 전송)</Typography.Text>}
      {value.map((w, i) => (
        <Space key={i} wrap>
          <Select virtual={false} mode="multiple" aria-label={`시간대 ${i + 1} 요일`} style={{ minWidth: 180 }} value={w.days}
            options={DAYS.map((d) => ({ value: d, label: DAY_LABEL[d] }))} onChange={(days: TransferWindow['days']) => set(i, { days })} />
          <Input aria-label={`시간대 ${i + 1} 시작 (HH:MM, 수집기 시간대)`} placeholder="시작 HH:MM" style={{ width: 110 }} value={w.start_local}
            onChange={(e) => set(i, { start_local: e.target.value })} />
          <Input aria-label={`시간대 ${i + 1} 끝 (HH:MM)`} placeholder="끝 HH:MM" style={{ width: 100 }} value={w.end_local}
            onChange={(e) => set(i, { end_local: e.target.value })} />
          <InputNumber aria-label={`시간대 ${i + 1} 최대 kbps`} min={1} placeholder="속도 제한 없음" style={{ width: 150 }} value={w.max_kbps ?? null}
            onChange={(v) => set(i, { max_kbps: v ?? null })} />
          <Button size="small" onClick={() => onChange(value.filter((_, j) => j !== i))}>시간대 {i + 1} 삭제</Button>
        </Space>
      ))}
      <Button size="small" onClick={() => onChange([...value, { days: [], start_local: '', end_local: '' }])}>시간대 추가</Button>
    </Space>
  )
}

function FieldInput({ field, value, onChange }: { field: FieldName; value: unknown; onChange: (v: unknown) => void }) {
  const label = FIELD_LABEL[field]
  switch (field) {
    case 'collection_mode':
    case 'local_copy_policy':
      return <Select virtual={false} aria-label={label} style={{ width: 210 }} placeholder={hint('선택')} value={value as string | undefined}
        options={(field === 'collection_mode' ? MODES : COPY).map((m) => ({ value: m, label: m }))} onChange={onChange} />
    case 'allowed_data_roles':
      return <Select virtual={false} mode="multiple" aria-label={label} style={{ minWidth: 210 }} placeholder={hint('하나 이상')}
        value={(value as string[] | undefined) ?? []} options={ROLES} onChange={onChange} />
    case 'local_retention_days':
    case 'expected_interval_s':
      return <InputNumber aria-label={label} min={field === 'expected_interval_s' ? 1 : 0} placeholder="없음" style={{ width: 140 }}
        value={(value as number | null | undefined) ?? null} onChange={(v) => onChange(v ?? null)} />
    case 'source_timezone':
      return <AutoComplete aria-label={label} style={{ width: 200 }} placeholder={hint('없음 (예: Asia/Seoul)')} value={(value as string | null | undefined) ?? ''}
        options={[{ value: 'Asia/Seoul' }, { value: 'UTC' }]} onChange={(v: string) => onChange(v ? v : null)} />
    case 'transfer_windows':
      return <WindowsInput value={(value as TransferWindow[] | undefined) ?? []} onChange={onChange} />
  }
}

type Edit = { kind: 'value'; value: unknown } | { kind: 'unset' }

// 초안 항목 표: 상태(카드 값·입력함·확인 필요)와 값. 고칠 수 있으면 바뀐 항목만 PATCH로 보낸다
function DraftEditor({ draft, editable, onSaved }: { draft: Draft; editable: boolean; onSaved: (d: Draft) => void }) {
  const { message } = App.useApp()
  const [edits, setEdits] = useState<Partial<Record<FieldName, Edit>>>({})
  const [note, setNote] = useState<string | undefined>(undefined)
  const canEdit = editable && draft.status === 'DRAFT'
  const save = useMutation({
    mutationFn: () => {
      const values: Record<string, unknown> = {}
      const unset: FieldName[] = []
      for (const f of FIELDS) {
        const e = edits[f]
        if (e?.kind === 'value') values[f] = e.value
        else if (e?.kind === 'unset') unset.push(f)
      }
      return patch<Draft>(`/v1/policy-drafts/${draft.policy_draft_id}`,
        { revision: draft.revision, values, needs_confirmation: unset, ...(note !== undefined ? { note: note || null } : {}) })
    },
    onSuccess: (d) => { message.success(`초안을 저장했습니다 (revision ${d.revision})`); onSaved(d) },
    onError: (e) => message.error(failText(e)),
  })
  const rows = FIELDS.map((f) => {
    const e = edits[f]
    const base = draft.fields[f]
    if (e?.kind === 'value') return { f, status: 'ENTERED', value: e.value, pointer: undefined }
    if (e?.kind === 'unset') return { f, status: 'NEEDS_CONFIRMATION', value: undefined, pointer: undefined }
    return { f, status: base.status, value: base.value, pointer: base.card_pointer }
  })
  const dirty = Object.keys(edits).length > 0 || note !== undefined
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Table size="small" pagination={false} rowKey="f" dataSource={rows} scroll={canEdit ? { x: 'max-content' } : undefined}
        columns={[
          { title: '항목', key: 'l', render: (_, r) => FIELD_LABEL[r.f] },
          { title: '상태', key: 's', render: (_, r) => (
            <Space direction="vertical" size={0}>
              {statusTag(FIELD_STATUS, r.status)}
              {r.pointer && <Typography.Text type="secondary" style={{ fontSize: 12, overflowWrap: 'anywhere' }}>카드 {r.pointer}</Typography.Text>}
            </Space>) },
          { title: '값', key: 'v', render: (_, r) => (canEdit
            ? <FieldInput field={r.f} value={r.status === 'NEEDS_CONFIRMATION' ? undefined : r.value}
                onChange={(v) => setEdits((x) => ({ ...x, [r.f]: { kind: 'value', value: v } }))} />
            : (r.status === 'NEEDS_CONFIRMATION' ? '-' : wrap(showValue(r.f, r.value)))) },
          ...(canEdit ? [{ title: '', key: 'a', render: (_: unknown, r: (typeof rows)[number]) => r.status !== 'NEEDS_CONFIRMATION' && (
            <Button size="small" onClick={() => setEdits((x) => ({ ...x, [r.f]: { kind: 'unset' } }))}>확인 필요로</Button>) }] : []),
        ]} />
      {canEdit ? (
        <>
          <Input.TextArea aria-label="초안 메모" rows={2} maxLength={1000} placeholder="메모 (운영자에게 전할 확인 결과 등)"
            value={note ?? draft.note ?? ''} onChange={(e) => setNote(e.target.value)} />
          <Space>
            <Button type="primary" disabled={!dirty} loading={save.isPending} onClick={() => save.mutate()}>초안 저장</Button>
            <Button disabled={!dirty} onClick={() => { setEdits({}); setNote(undefined) }}>되돌리기</Button>
          </Space>
        </>
      ) : draft.note && <Typography.Text>메모: {draft.note}</Typography.Text>}
    </Space>
  )
}

function ChangeList({ changes }: { changes: Change[] }) {
  if (!changes.length) return <Typography.Text type="secondary">바뀌는 값 없음</Typography.Text>
  return (
    <ul style={{ margin: 0, paddingInlineStart: 20, overflowWrap: 'anywhere' }}>
      {changes.map((c) => <li key={c.field}>{FIELD_LABEL[c.field]}: {showValue(c.field, c.before)} → {showValue(c.field, c.after)}</li>)}
    </ul>
  )
}

// 지금 정책과 비교 (운영자 검토 화면과 같은 미리 보기)
function PolicyPreview({ preview, deviceName }: { preview: Preview; deviceName: (id: string) => string }) {
  const changed = new Set(preview.changes.map((c) => c.field))
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Typography.Text strong>
        지금 정책과 비교 — 수집기 {deviceName(preview.collector_device_id)},{' '}
        {preview.current_policy_version ? `저장된 정책 v${preview.current_policy_version}` : '저장된 정책 없음 (장비 기록으로 만든 기본 정책)'}
      </Typography.Text>
      {!preview.source_in_current_policy && <Alert type="info" showIcon message="지금 정책에 이 장비 항목이 없어 적용하면 새로 들어갑니다 (지금 값은 장비 기록 기준)" />}
      {preview.blockers.length > 0 && (
        <Alert type="error" showIcon message="이대로는 적용할 수 없습니다"
          description={<ul style={{ margin: 0 }}>{preview.blockers.map((b, i) => <li key={i}>{b.condition} ({b.actual ?? '-'})</li>)}</ul>} />
      )}
      <Table size="small" pagination={false} rowKey="f" dataSource={FIELDS.map((f) => ({ f }))}
        columns={[
          { title: '항목', key: 'l', render: (_, r) => FIELD_LABEL[r.f] },
          { title: '지금', key: 'c', render: (_, r) => wrap(showValue(r.f, preview.current[r.f])) },
          { title: '적용 후', key: 'p', render: (_, r) => (changed.has(r.f)
            ? <Typography.Text strong>{wrap(showValue(r.f, preview.proposed[r.f]))} <Tag color="blue">바뀜</Tag></Typography.Text>
            : <Typography.Text type="secondary">같음</Typography.Text>) },
        ]} />
    </Space>
  )
}

function DraftDetail({ draft, editable, onChange, deviceName }: {
  draft: Draft; editable: boolean; onChange: (d: Draft) => void; deviceName: (id: string) => string
}) {
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Space wrap>
        {statusTag(DRAFT_STATUS, draft.status)}
        <Typography.Text>장비 {deviceName(draft.device_id)} · 수집기 {deviceName(draft.collector_device_id)} · revision {draft.revision}
          {draft.integration_card_id ? ` · 카드 v${draft.integration_card_version}` : ' · 카드 없이 작성'}</Typography.Text>
      </Space>
      {draft.status === 'DRAFT' && (
        <Alert type="info" showIcon message="운영자가 확인해 적용하기 전에는 장비 기록·장비 정책이 바뀌지 않습니다."
          description={draft.needs_confirmation.length
            ? `확인 필요 ${draft.needs_confirmation.length}개: ${draft.needs_confirmation.map((f) => FIELD_LABEL[f]).join(', ')} — 적용해도 지금 값을 그대로 둡니다.`
            : '확인 필요 항목이 없습니다.'} />
      )}
      <DraftEditor key={`${draft.policy_draft_id}:${draft.revision}`} draft={draft} editable={editable} onSaved={onChange} />
      {draft.preview && <PolicyPreview preview={draft.preview} deviceName={deviceName} />}
      {draft.status === 'APPLIED' && draft.applied && (
        <Alert type="success" showIcon
          message={`적용됨 — 수집기 정책 v${draft.applied.device_policy_version} (${draft.applied.policy_version}) · ${kst(draft.decided_at)}`}
          description={<><ChangeList changes={draft.applied.changes} />{draft.decision_reason && <div>사유: {draft.decision_reason}</div>}</>} />
      )}
      {draft.status === 'REJECTED' && (
        <Alert type="error" showIcon message={`반려됨 · ${kst(draft.decided_at)}`} description={`사유: ${draft.decision_reason ?? '-'}`} />
      )}
    </Space>
  )
}

function useNames() {
  const devices = useQuery({ queryKey: ['devices', 'all'], queryFn: () => api<{ items: Device[] }>('/v1/devices?limit=500') })
  const name = (id: string) => devices.data?.items.find((d) => d.device_id === id)?.name ?? id
  return { devices, name }
}

// ORG_ADMIN: 카드에서 시작하는 마법사 (카드 확인 → 장비 선택 → 정책 초안)
export function OnboardingWizard({ card, onClose }: { card: CardLike; onClose: () => void }) {
  const { message } = App.useApp()
  const qc = useQueryClient()
  const { devices, name } = useNames()
  const open = useQuery({ queryKey: ['policy-drafts', 'DRAFT'], queryFn: () => api<DraftPage>('/v1/policy-drafts?status=DRAFT&limit=500') })
  const [step, setStep] = useState(0)
  const [deviceId, setDeviceId] = useState<string>()
  const [draft, setDraft] = useState<Draft>()
  const openFor = (id?: string) => open.data?.items.find((d) => d.device_id === id)
  const start = useMutation({
    mutationFn: () => {
      const existing = openFor(deviceId)
      return existing ? api<Draft>(`/v1/policy-drafts/${existing.policy_draft_id}`)
        : post<Draft>('/v1/policy-drafts', { device_id: deviceId, integration_card_id: card.integration_card_id, integration_card_version: card.version })
    },
    onSuccess: (d) => { setDraft(d); setStep(2); void qc.invalidateQueries({ queryKey: ['policy-drafts'] }) },
    onError: (e) => message.error(failText(e)),
  })
  const equipment = (Array.isArray(card.content.equipment) ? card.content.equipment : []) as
    { kind?: string; manufacturer?: string; model?: string; firmware?: string; count?: number }[]
  const placeable = (d: Device) => d.kind === 'COLLECTOR' || !!d.collector_device_id
  return (
    <Modal open width={980} title={`장비 정책 초안 — ${card.content.basic?.project_name ?? ''} (카드 v${card.version})`} onCancel={onClose} footer={null} destroyOnClose>
      <Steps current={step} size="small" style={{ marginBottom: 16 }} items={[{ title: '카드 확인' }, { title: '장비 선택' }, { title: '정책 초안' }]} />
      {step === 0 && (
        <Space direction="vertical" style={{ width: '100%' }}>
          <CardChecklist card={card} />
          <Typography.Text strong>카드의 장비 항목 (참고)</Typography.Text>
          <Table size="small" pagination={false} rowKey={(r) => equipment.indexOf(r).toString()} dataSource={equipment} locale={{ emptyText: '장비 항목이 없습니다' }}
            columns={[{ title: '종류', dataIndex: 'kind' }, { title: '제조사', dataIndex: 'manufacturer' }, { title: '모델', dataIndex: 'model' },
              { title: '펌웨어', dataIndex: 'firmware' }, { title: '수량', dataIndex: 'count' }]} />
          <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
            원천 장비·수집기 등록과 매핑은 '장비' 화면에서 합니다 (카드의 장비 항목은 자유 기록이라 장비를 자동으로 만들지 않습니다).
            카드에서 초안으로 가져오는 값은 희망 수집 수준·현장 사본 정책·현장 보존 기간뿐이고, 허용 data_role·기대 수집 주기·전송 허용 시간대·
            source_timezone은 직접 넣거나 '확인 필요'로 남깁니다.
          </Typography.Paragraph>
          <Button type="primary" onClick={() => setStep(1)}>다음: 장비 선택</Button>
        </Space>
      )}
      {step === 1 && (
        <Space direction="vertical" style={{ width: '100%' }}>
          <Table size="small" rowKey="device_id" loading={devices.isLoading} dataSource={devices.data?.items ?? []} pagination={false} scroll={{ x: 'max-content' }}
            rowSelection={{ type: 'radio', selectedRowKeys: deviceId ? [deviceId] : [], onChange: (keys) => setDeviceId(String(keys[0])),
              getCheckboxProps: (d) => ({ disabled: !placeable(d), 'aria-label': `${d.name} 선택` }) }}
            columns={[{ title: '장비', dataIndex: 'name' },
              { title: '종류', dataIndex: 'kind', render: (k: string) => (k === 'COLLECTOR' ? '수집기' : '원천 장비') },
              { title: '수집기', key: 'c', render: (_, d) => (d.kind === 'COLLECTOR' ? '(자신)' : d.collector_device_id ? name(d.collector_device_id) : '매핑 없음 — 초안 불가') },
              { title: '지금 수집 수준', dataIndex: 'collection_mode' }, { title: '현장 사본', dataIndex: 'local_copy_policy' },
              { title: '초안', key: 'o', render: (_, d) => (openFor(d.device_id) ? <Tag color="blue">검토 중</Tag> : '-') }]} />
          <Space>
            <Button onClick={() => setStep(0)}>이전</Button>
            <Button type="primary" disabled={!deviceId} loading={start.isPending} onClick={() => start.mutate()}>
              {openFor(deviceId) ? '검토 중인 초안 열기' : '카드로 초안 만들기'}
            </Button>
          </Space>
        </Space>
      )}
      {step === 2 && draft && (
        <DraftDetail draft={draft} editable deviceName={name}
          onChange={(d) => { setDraft(d); void qc.invalidateQueries({ queryKey: ['policy-drafts'] }) }} />
      )}
    </Modal>
  )
}

// 초안 목록: 제공기관(mode=org, 열기·고치기)과 운영자(mode=operator, 검토·적용·반려)
export function PolicyDraftList({ mode, canEdit }: { mode: 'org' | 'operator'; canEdit: boolean }) {
  const { message } = App.useApp()
  const qc = useQueryClient()
  const { name } = useNames()
  const [status, setStatus] = useState<string>(mode === 'operator' ? 'DRAFT' : '')
  const list = useQuery({ queryKey: ['policy-drafts', status || 'all'],
    queryFn: () => api<DraftPage>(`/v1/policy-drafts?limit=200${status ? `&status=${status}` : ''}`) })
  const cards = useQuery({ queryKey: ['integration-cards'], queryFn: () => api<{ items: IntegrationCard[] }>('/v1/integration-cards') })
  const [opened, setOpened] = useState<Draft>()
  const [reason, setReason] = useState('')
  const cardName = (id: string | null) => (id ? cards.data?.items.find((c) => c.integration_card_id === id)?.content.basic.project_name ?? id : '-')
  const refresh = (d?: Draft) => { if (d) setOpened(d); void qc.invalidateQueries({ queryKey: ['policy-drafts'] }) }
  const openDraft = useMutation({
    mutationFn: (id: string) => api<Draft>(`/v1/policy-drafts/${id}`),
    onSuccess: (d) => { setReason(''); setOpened(d) }, onError: (e) => message.error(failText(e)),
  })
  const decide = useMutation({
    mutationFn: ({ d, kind }: { d: Draft; kind: 'apply' | 'reject' }) => (kind === 'apply'
      ? post<Draft>(`/v1/policy-drafts/${d.policy_draft_id}/apply`, {
        revision: d.revision, expected_policy_version: d.preview?.current_policy_version ?? null, // 본 미리보기 기준 (바뀌었으면 409)
        ...(reason.trim() ? { reason: reason.trim() } : {}),
      })
      : post<Draft>(`/v1/policy-drafts/${d.policy_draft_id}/reject`, { reason: reason.trim() })),
    onSuccess: (d) => {
      message.success(d.status === 'APPLIED' ? `적용했습니다 — 수집기 정책 v${d.applied?.device_policy_version} (${d.applied?.policy_version})` : '반려했습니다')
      refresh(d)
      void qc.invalidateQueries({ queryKey: ['devices'] })
    },
    onError: (e) => message.error(failText(e)),
  })
  return (
    <Card className="policy-draft-list" title={mode === 'operator' ? '장비 정책 초안 검토 (온보딩)' : '장비 정책 초안'}
      extra={mode === 'operator' && (
        <Select virtual={false} aria-label="초안 상태" style={{ width: 160 }} value={status} onChange={setStatus}
          options={[{ value: 'DRAFT', label: '확인 전' }, { value: 'APPLIED', label: '적용됨' }, { value: 'REJECTED', label: '반려됨' }, { value: '', label: '전체' }]} />
      )}>
      <Typography.Paragraph type="secondary">
        {mode === 'operator'
          ? '기관이 연계 카드로 만든 초안을 지금 정책과 비교해 적용하거나 반려합니다 (다중 인증). 적용하면 수집기 정책 새 버전이 배포되고 장비 기록이 바뀝니다.'
          : '연계 카드의 "정책 초안"에서 만듭니다. 운영자가 확인해 적용하기 전에는 장비·정책이 바뀌지 않습니다.'}
      </Typography.Paragraph>
      <Table rowKey="policy_draft_id" size="small" loading={list.isLoading} dataSource={list.data?.items ?? []} scroll={{ x: 'max-content' }}
        locale={{ emptyText: '초안이 없습니다' }}
        columns={[
          ...(mode === 'operator' ? [{ title: '기관', dataIndex: 'organization_id' }] : []),
          { title: '장비', key: 'd', render: (_, d) => name(d.device_id) },
          { title: '연계 카드', key: 'c', render: (_, d) => (d.integration_card_id ? `${cardName(d.integration_card_id)} v${d.integration_card_version}` : '-') },
          { title: '상태', key: 's', render: (_, d) => statusTag(DRAFT_STATUS, d.status) },
          { title: '확인 필요', key: 'n', render: (_, d) => (d.needs_confirmation.length ? <Tag color="orange">{d.needs_confirmation.length}개</Tag> : <Tag color="green">없음</Tag>) },
          { title: '수정', dataIndex: 'updated_at', render: kst },
          { title: '', key: 'a', render: (_, d) => (
            <Button size="small" onClick={() => openDraft.mutate(d.policy_draft_id)}>{mode === 'operator' && d.status === 'DRAFT' ? '검토' : '열기'}</Button>) },
        ]} />
      <Modal open={!!opened} width={980} title="장비 정책 초안" onCancel={() => setOpened(undefined)} footer={null} destroyOnClose>
        {opened && (
          <Space direction="vertical" style={{ width: '100%' }}>
            <DraftDetail draft={opened} editable={canEdit} deviceName={name} onChange={refresh} />
            {mode === 'operator' && opened.status === 'DRAFT' && (
              <Card size="small" title="운영자 확인">
                <Space direction="vertical" style={{ width: '100%' }}>
                  <Input.TextArea aria-label="적용·반려 사유" rows={2} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)}
                    placeholder="사유 (반려는 필수, 적용은 선택)" />
                  <Space wrap>
                    <Button type="primary" loading={decide.isPending} disabled={(opened.preview?.blockers.length ?? 0) > 0}
                      onClick={() => decide.mutate({ d: opened, kind: 'apply' })}>적용 (다중 인증, revision {opened.revision})</Button>
                    <Button danger loading={decide.isPending} disabled={reason.trim().length < 2}
                      onClick={() => decide.mutate({ d: opened, kind: 'reject' })}>반려</Button>
                  </Space>
                </Space>
              </Card>
            )}
          </Space>
        )}
      </Modal>
    </Card>
  )
}
