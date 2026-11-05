// 화면 표시는 KST (09번 요구사항 11.1), 저장은 UTC.
const KST = new Intl.DateTimeFormat('ko-KR', {
  timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
})

export const kst = (iso: string | null | undefined) => (iso ? `${KST.format(new Date(iso))} KST` : '-')

const KST_DAY = new Intl.DateTimeFormat('ko-KR', { timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit' })
const KST_TIME = new Intl.DateTimeFormat('ko-KR', { timeZone: 'Asia/Seoul', hour: '2-digit', minute: '2-digit' })

// 구간 표시: 같은 날이면 끝은 시각만 ("2026. 11. 03. 오전 10:30 ~ 오전 11:45 KST"). 끝이 없으면 시작만
export function kstRange(from: string | null | undefined, to: string | null | undefined): string {
  if (!from) return '-'
  if (!to) return kst(from)
  const a = new Date(from), b = new Date(to)
  return KST_DAY.format(a) === KST_DAY.format(b) ? `${KST.format(a)} ~ ${KST_TIME.format(b)} KST` : `${kst(from)} ~ ${kst(to)}`
}

export function bytes(n: number | null | undefined): string {
  if (n == null) return '-'
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`
}

// 화면에는 L0~L4 라벨, 저장·API는 코드값 (domain.md 3장)
export const SCOPE_LABEL: Record<string, string> = {
  PRIVATE: 'L0 비공개', PROGRAM_REVIEW: 'L1 관리기관 검토', SANDBOX_PARTNERS: 'L2 특구 참여기관',
  APPROVED_PARTNERS: 'L3 승인 이용기관', PUBLIC: 'L4 공개',
}

export const STATUS_LABEL: Record<string, string> = {
  UPLOADING: '업로드 중', RECEIVED: '수신(검증 대기)', VALIDATING: '검증 중', REGISTERED: '등록됨',
  PROXIED: '프록시 완료', READY_PRIVATE: '사용 가능', QUARANTINED: '격리', REJECTED: '폐기', FAILED: '처리 실패',
}

export const QUARANTINE_LABEL: Record<string, string> = {
  CHECKSUM_MISMATCH: '전송 중 손상(해시 불일치)', MISSING_REQUIRED_METADATA: '필수 메타데이터 누락',
  UNKNOWN_SESSION: '회차 미상', UNSUPPORTED_FORMAT: '지원하지 않는 형식', CORRUPT_FILE: '손상 파일',
  MIME_MISMATCH: '형식 불일치', MALWARE_SUSPECTED: '악성코드 의심',
}

// 감시 규칙 (11번). 코드값은 API, 화면은 한국어
export const RULE_LABEL: Record<string, string> = {
  HEARTBEAT_MISSING: '하트비트 무응답', INGEST_LAG: '수집 지연', SESSION_NO_DATA: '실증 예정일 무수집',
  VOLUME_DROP: '수집량 급감', UPLOAD_FAILURE_RATE: '업로드 실패율', PROCESSING_BACKLOG: '처리 대기',
  QUARANTINE_SURGE: '격리 급증', STORAGE_FORECAST: '용량 예측', OPENAPI_FAILURE: 'Open API 실패',
  OBSERVATION_LAG: '관측값 지연', CLOCK_OFFSET: '시계 오차', LOCAL_DISK_LOW: '현장 저장 부족', HARDWARE_SIGNAL: '하드웨어 경고',
}

export const SEVERITY_COLOR: Record<string, string> = { INFO: 'blue', WARNING: 'orange', CRITICAL: 'red' }

// 실증 이벤트 공통 상위분류 (contracts/codes/event-categories.json, 기준 문서 7.2장). 기관 하위코드는 '상위.하위'
export const EVENT_CATEGORY_LABEL: Record<string, string> = {
  VESSEL: '선박', PERSON: '사람', FLOATING_OBJECT: '부유물', WEATHER_WAVE: '기상·파랑', EQUIPMENT_FAULT: '장비 장애', SAFETY_INCIDENT: '안전사고',
}

export function eventLabel(code: string): string {
  const dot = code.indexOf('.')
  const top = dot < 0 ? code : code.slice(0, dot)
  return `${EVENT_CATEGORY_LABEL[top] ?? top}${dot < 0 ? '' : ` · ${code.slice(dot + 1)}`}`
}

export const EVENT_SOURCE_LABEL: Record<string, string> = { DEVICE: '장비', MANUAL: '수동 입력', MODEL: '모델', RULE: '규칙' }

export function duration(s: number | null | undefined): string {
  if (s == null) return '-'
  if (s < 60) return `${s}초`
  if (s < 3600) return `${Math.floor(s / 60)}분`
  if (s < 86400) return `${Math.floor(s / 3600)}시간 ${Math.floor((s % 3600) / 60)}분`
  return `${Math.floor(s / 86400)}일 ${Math.floor((s % 86400) / 3600)}시간`
}
