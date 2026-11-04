// 화면 표시는 KST (09번 요구사항 11.1), 저장은 UTC.
const KST = new Intl.DateTimeFormat('ko-KR', {
  timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
})

export const kst = (iso: string | null | undefined) => (iso ? `${KST.format(new Date(iso))} KST` : '-')

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
