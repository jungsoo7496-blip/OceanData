// 플랫폼 API 클라이언트. 같은 출처의 /v1을 부른다 (nginx·vite가 API로 프록시).
import { currentUser, login } from './auth'

export interface Problem {
  type?: string
  title: string
  status: number
  detail?: string
  code: string
  request_id: string
  errors?: { pointer: string; code: string; message: string }[]
  unmet_conditions?: { condition: string; actual?: string | null }[]
  allowed_variants?: string[]
}

export class ApiError extends Error {
  constructor(public problem: Problem) {
    super(problem.detail ?? problem.title)
  }
  get code() {
    return this.problem.code
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const user = await currentUser()
  if (!user) {
    await login()
    throw new Error('로그인이 필요합니다')
  }
  const headers = new Headers(init.headers)
  headers.set('Authorization', `Bearer ${user.access_token}`)
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const res = await fetch(path, { ...init, headers })
  if (res.status === 204) return undefined as T
  const body = await res.json().catch(() => ({}))
  if (!res.ok) {
    if (res.status === 401) void login()
    throw new ApiError({ status: res.status, title: res.statusText, code: 'INTERNAL', request_id: '', ...body })
  }
  return body as T
}

export const post = <T>(path: string, body: unknown, headers?: Record<string, string>) =>
  api<T>(path, { method: 'POST', body: JSON.stringify(body), headers })
export const patch = <T>(path: string, body: unknown) => api<T>(path, { method: 'PATCH', body: JSON.stringify(body) })

export interface Page<T> {
  items: T[]
  next_cursor: string | null
}

export interface Me {
  principal_kind: string
  organization_id: string
  org_type: string
  roles: string[]
  mfa: boolean
  user_id?: string
}

export interface Device {
  device_id: string
  name: string
  kind: 'SOURCE' | 'COLLECTOR'
  device_type: string
  status: string
  collection_mode: string
  local_copy_policy: string
  center_copy_may_be_only_copy: boolean
  collector_device_id?: string
  project_id?: string
  last_heartbeat_at: string | null
  expected_interval_s: number | null
}

export interface Session {
  session_id: string
  project_id: string
  label: string
  title: string
  starts_at: string
  ends_at: string | null
  site: string | null
  note: string | null
  device_ids: string[]
}

export interface Asset {
  asset_id: string
  project_id: string
  session_id?: string | null
  source_system_id: string
  data_type: string
  variant: string
  data_role: string
  capture_start_at: string | null
  ingested_at: string
  timestamp_quality: { source: string; verified: boolean }
  mime_type: string
  byte_size: number
  sharing_scope: string
  ingest_status: string
  quarantine_reason?: string
  quality_status: string
  quality_reasons: string[]
  has_binary: boolean
  provenance: { original_filename?: string; original_relative_path?: string }
  video_ext?: { resolution?: string; duration_s?: number; fps?: number; codec?: string }
}

export interface AgentHealth {
  device_id: string
  status: 'OK' | 'STALE' | 'ERROR' | 'NEVER_SEEN'
  last_heartbeat_at: string | null
  last_registered_at: string | null
  queue_items: number
  last_error_code: string | null
}

export interface OrgUsage {
  organization_id: string
  quota_bytes: number
  usage_bytes: number
  usage_ratio: number
  breakdown: { stored_bytes: number; quarantine_bytes: number; reserved_bytes: number; backup_bytes: number }
}

export interface Capabilities {
  gpu_enabled: boolean
  processing_steps: string[]
  report_formats: string[]
}
