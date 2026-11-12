// 웹 업로드 (09번 요구사항 8): Uppy + @uppy/aws-s3 멀티파트를 04번 업로드 API에 연결한다.
// 1) 브라우저에서 SHA-256을 스트리밍으로 계산 → 2) POST /v1/uploads (매니페스트) → 3) 사전 서명 URL로 파트 전송
// → 4) complete. 탭을 닫았다가 같은 파일을 다시 고르면 서버가 진행 중 세션을 돌려주고, 이미 보낸 파트는 건너뛴다.
import '@uppy/core/dist/style.min.css'
import '@uppy/dashboard/dist/style.min.css'
import { useQuery } from '@tanstack/react-query'
import AwsS3, { type AwsS3Part } from '@uppy/aws-s3'
import Uppy, { type UppyFile } from '@uppy/core'
import Dashboard from '@uppy/dashboard'
import ko_KR from '@uppy/locales/lib/ko_KR'
import { Alert, Card, Form, Grid, Select, Space, Table, Tag, Typography } from 'antd'
import { createSHA256 } from 'hash-wasm'
import { useEffect, useMemo, useRef, useState } from 'react'
import { api, ApiError, post, type Device, type Page, type Session } from '../lib/api'
import { explain } from '../lib/problems'

type Meta = { uploadId?: string; assetId?: string; partSize?: number; sha?: string } & Record<string, unknown>
type Body = Record<string, never>
type F = UppyFile<Meta, Body>

interface Created {
  status: 'UPLOAD_CREATED' | 'METADATA_ONLY_REGISTERED' | 'ALREADY_REGISTERED'
  upload_id?: string
  asset_id: string
  part_size?: number
  part_count?: number
  parts?: { part_number: number; url: string }[]
}

interface Row { name: string; status: string; detail?: string; color: string }

const URL_TTL_MS = 10 * 60 * 1000 // 서버 기본 15분보다 짧게 새로 받는다

function dataTypeOf(mime: string): string {
  if (mime.startsWith('video/')) return 'VIDEO'
  if (mime.startsWith('image/')) return 'IMAGE'
  if (mime === 'text/csv' || mime === 'application/json') return 'SENSOR'
  return 'DOCUMENT'
}

async function sha256(file: Blob, onProgress: (v: number) => void): Promise<string> {
  const h = await createSHA256()
  const step = 8 * 1024 * 1024
  for (let off = 0; off < file.size; off += step) {
    h.update(new Uint8Array(await file.slice(off, off + step).arrayBuffer())) // 전체를 메모리에 올리지 않는다
    onProgress(Math.min(1, (off + step) / file.size))
  }
  return h.digest('hex')
}

export function Upload() {
  const devices = useQuery({ queryKey: ['devices'], queryFn: () => api<Page<Device>>('/v1/devices?limit=500') })
  const sessions = useQuery({ queryKey: ['sessions'], queryFn: () => api<Page<Session>>('/v1/sessions?limit=500') })
  const [source, setSource] = useState<string>()
  const [variant, setVariant] = useState('ORIGINAL')
  const [session, setSession] = useState<string>()
  const [rows, setRows] = useState<Row[]>([])
  const target = useRef<HTMLDivElement>(null)
  const ctx = useRef({ source, variant, session })
  ctx.current = { source, variant, session }
  const addRow = (r: Row) => setRows((x) => [r, ...x.filter((y) => y.name !== r.name)])
  const narrow = Grid.useBreakpoint().sm === false // 휴대폰 폭: 이름을 입력 위로 (선택 상자가 카드 밖으로 나가지 않게)

  const uppy = useMemo(() => {
    const urls = new Map<string, { at: number; byPart: Map<number, string> }>()
    // Uppy의 getChunkSize는 UppyFile이 아닌 file.data(Blob)를 전달한다.
    const partSizes = new WeakMap<object, number>()
    const refreshUrls = async (uploadId: string) => {
      const pl = await api<{ pending_parts: { part_number: number; url: string }[] }>(`/v1/uploads/${uploadId}/parts`)
      urls.set(uploadId, { at: Date.now(), byPart: new Map(pl.pending_parts.map((p) => [p.part_number, p.url])) })
    }
    // 한국어 문구 (09번 요구사항 11.1) — @uppy/locales는 Uppy 같은 저장소의 문구 묶음(MIT, ADR-0016)
    const u = new Uppy<Meta, Body>({ autoProceed: false, locale: ko_KR, restrictions: { maxNumberOfFiles: 50 } })
    u.addPreProcessor(async (ids) => {
      for (const id of ids) {
        const file = u.getFile(id) as F
        const { source: src, variant: v, session: ses } = ctx.current
        try {
          if (!src) throw new Error('원천 장비를 고르세요')
          const sha = await sha256(file.data as Blob, (value) =>
            u.emit('preprocess-progress', file, { mode: 'determinate', message: '해시 계산 중', value }))
          const mime = file.type || 'application/octet-stream'
          const manifest = {
            schema_version: '1.0.0', source_system_id: src, data_type: dataTypeOf(mime), variant: v,
            ...(ses ? { session_id: ses } : {}),
            file: { original_filename: file.name, byte_size: file.size, checksum_sha256: sha, mime_type: mime,
              ...((file.data as File).webkitRelativePath ? { original_relative_path: (file.data as File).webkitRelativePath } : {}) },
            timestamp_quality: { source: 'UNKNOWN', verified: false }, // 촬영시각은 서버가 파일 내부 메타에서 찾는다
            client: { kind: 'WEB', version: '0.1.0' },
          }
          // 중복은 서버가 (기관·장비·sha256)으로 판단한다. Idempotency-Key를 파일 해시로 고정하면 다시 고를 때
          // 첫 응답(끝난 세션)이 그대로 재생되므로 붙이지 않는다
          const c = await post<Created>('/v1/uploads', manifest)
          if (c.status !== 'UPLOAD_CREATED') {
            addRow({ name: file.name ?? id, color: 'green', status: c.status === 'ALREADY_REGISTERED' ? '이미 등록된 파일' : '메타데이터만 등록', detail: c.asset_id })
            u.removeFile(id)
            continue
          }
          if (!c.part_size) throw new Error('서버의 업로드 파트 크기를 확인하지 못했습니다')
          partSizes.set(file.data, c.part_size)
          urls.set(c.upload_id!, { at: Date.now(), byPart: new Map((c.parts ?? []).map((p) => [p.part_number, p.url])) })
          u.setFileMeta(id, { uploadId: c.upload_id, assetId: c.asset_id, partSize: c.part_size, sha })
          // 이미 있는 세션이면 Uppy가 listParts로 받은 파트를 건너뛴다 (재개)
          u.setFileState(id, { s3Multipart: { uploadId: c.upload_id!, key: c.asset_id } } as Partial<F>)
          u.emit('preprocess-complete', file)
        } catch (e) {
          const help = e instanceof ApiError ? explain(e.code) : { cause: String((e as Error).message ?? e), action: '' }
          const extra = e instanceof ApiError && e.problem.allowed_variants ? ` (허용 변형: ${e.problem.allowed_variants.join(', ')})` : ''
          addRow({ name: file.name ?? id, color: 'red', status: help.cause + extra, detail: help.action })
          u.removeFile(id)
        }
      }
    })
    u.use(AwsS3<Meta, Body>, {
      shouldUseMultipart: true,
      limit: 4,
      // 같은 Blob에 연결한 서버 파트 크기를 사용해 신규·재개 전송의 경계를 일치시킨다.
      getChunkSize: (data) => {
        const size = partSizes.get(data)
        if (!size) throw new Error('서버의 업로드 파트 크기를 확인하지 못했습니다')
        return size
      },
      createMultipartUpload: async (file) => ({ uploadId: file.meta.uploadId as string, key: file.meta.assetId as string }),
      listParts: async (_file, { uploadId }) => {
        const pl = await api<{ completed_parts: { part_number: number; etag: string; size: number }[] }>(`/v1/uploads/${uploadId}/parts`)
        return pl.completed_parts.map((p): AwsS3Part => ({ PartNumber: p.part_number, ETag: p.etag, Size: p.size }))
      },
      signPart: async (_file, { uploadId, partNumber }) => {
        let entry = urls.get(uploadId)
        if (!entry || Date.now() - entry.at > URL_TTL_MS || !entry.byPart.has(partNumber)) {
          await refreshUrls(uploadId)
          entry = urls.get(uploadId)
        }
        return { url: entry!.byPart.get(partNumber)!, method: 'PUT' }
      },
      abortMultipartUpload: async (_file, { uploadId }) => { await api(`/v1/uploads/${uploadId}`, { method: 'DELETE' }) },
      completeMultipartUpload: async (file, { uploadId, parts }) => {
        await post(`/v1/uploads/${uploadId}/complete`, {
          parts: parts.map((p) => ({ part_number: p.PartNumber, etag: p.ETag })),
        }, { 'Idempotency-Key': `complete-${uploadId}` })
        addRow({ name: file.name ?? '', color: 'blue', status: '수신 완료 — 무결성 검증 중', detail: file.meta.assetId as string })
        return {}
      },
    })
    // 결과는 아래 표에 남기고, 업로더는 비워 다음 파일을 받을 수 있게 한다
    u.on('complete', () => setTimeout(() => u.cancelAll(), 1500))
    u.on('upload-error', (file, err) => {
      if (file) addRow({ name: file.name ?? '', color: 'red', status: '전송 실패', detail: `${err.message} — 다시 시도하면 남은 파트만 보냅니다` })
    })
    return u
  }, [])

  useEffect(() => {
    if (!target.current) return
    // 폭은 카드에 맞춘다 (기본 750px은 휴대폰 폭에서 문서를 넓힌다, 요구사항 11.2)
    uppy.use(Dashboard<Meta, Body>, { inline: true, target: target.current, width: '100%', height: 360, proudlyDisplayPoweredByUppy: false,
      note: '파일을 끌어 놓거나 고르세요. 큰 파일은 나누어 바로 저장소로 보냅니다.' })
    return () => {
      const d = uppy.getPlugin('Dashboard')
      if (d) uppy.removePlugin(d)
    }
  }, [uppy])

  const sources = (devices.data?.items ?? []).filter((d) => d.kind === 'SOURCE' || d.kind === 'COLLECTOR')
  const selected = sources.find((d) => d.device_id === source)
  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card title="업로드">
        <Form layout={narrow ? 'vertical' : 'inline'} style={{ marginBottom: 16, rowGap: 8 }}>
          <Form.Item label="원천 장비" required>
            <Select virtual={false} style={{ width: 240, maxWidth: '100%' }} value={source} onChange={setSource} aria-label="원천 장비"
              options={sources.map((d) => ({ value: d.device_id, label: `${d.name} (${d.collection_mode.slice(0, 2)})` }))} />
          </Form.Item>
          <Form.Item label="변형">
            <Select virtual={false} style={{ width: 160 }} value={variant} onChange={setVariant} aria-label="변형"
              options={['ORIGINAL', 'CLIP', 'PROXY', 'SAMPLE_FRAME'].map((v) => ({ value: v, label: v }))} />
          </Form.Item>
          <Form.Item label="실증회차">
            <Select virtual={false} allowClear style={{ width: 240, maxWidth: '100%' }} value={session} onChange={setSession} aria-label="실증회차"
              placeholder="모르면 비워 두세요 (회차 미상으로 보관)"
              options={(sessions.data?.items ?? []).map((s) => ({ value: s.session_id, label: `${s.label} ${s.title}` }))} />
          </Form.Item>
        </Form>
        {selected?.collection_mode === 'C2_EVENT_CLIPS' && variant === 'ORIGINAL' && (
          <Alert style={{ marginBottom: 16 }} type="warning" showIcon message="이 장비는 이벤트 클립(C2)만 받습니다. 변형을 CLIP으로 고르세요." />
        )}
        <div ref={target} />
        <Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>
          업로드가 끊기면 같은 파일을 다시 고르세요. 이미 보낸 부분은 건너뜁니다 (7일 안).
        </Typography.Paragraph>
      </Card>
      <Card title="결과">
        <Table rowKey="name" size="small" dataSource={rows} pagination={false} locale={{ emptyText: '아직 없습니다' }}
          columns={[{ title: '파일', dataIndex: 'name' }, { title: '결과', key: 's', render: (_, r) => <Tag color={r.color}>{r.status}</Tag> },
            { title: '자세히', dataIndex: 'detail' }]} />
      </Card>
    </Space>
  )
}
