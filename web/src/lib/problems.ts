// 오류 코드별 원인과 다음 행동 (09번 요구사항 8.3). 프로그램 분기는 code로 한다 (api-conventions.md).
export const PROBLEM_HELP: Record<string, { cause: string; action: string }> = {
  VALIDATION_ERROR: { cause: '입력값 형식이 맞지 않습니다', action: '표시된 항목을 고친 뒤 다시 시도하세요' },
  UNAUTHENTICATED: { cause: '로그인이 만료되었습니다', action: '다시 로그인하세요' },
  FORBIDDEN: { cause: '이 작업을 할 권한이 없습니다', action: '기관 관리자에게 문의하세요' },
  MFA_REQUIRED: { cause: '다중 인증(OTP)이 필요한 작업입니다', action: 'OTP로 다시 로그인한 뒤 시도하세요' },
  NOT_FOUND: { cause: '대상을 찾을 수 없습니다', action: '목록을 새로 고친 뒤 다시 선택하세요' },
  CONFLICT: { cause: '현재 상태에서 할 수 없는 작업입니다', action: '잠시 뒤 다시 시도하거나 상태를 확인하세요' },
  IDEMPOTENCY_CONFLICT: { cause: '같은 요청 키로 다른 내용이 전송되었습니다', action: '페이지를 새로 고친 뒤 다시 시도하세요' },
  INVALID_STATE_TRANSITION: { cause: '자산 상태가 바뀌어 이 작업을 할 수 없습니다', action: '상태를 확인하세요' },
  QUOTA_EXCEEDED: { cause: '기관 저장 쿼터를 넘습니다', action: '불필요한 자료를 정리하거나 운영기관에 쿼터 증설을 문의하세요' },
  GONE: { cause: '업로드 세션이 만료되었습니다', action: '파일을 다시 선택해 처음부터 올리세요' },
  PAYLOAD_TOO_LARGE: { cause: '요청이 너무 큽니다', action: '나누어 보내세요' },
  RULE_VIOLATION: { cause: '수집·공유 정책 조건을 충족하지 않습니다', action: '안내된 조건(허용 변형, 회차 지정 등)을 확인하세요' },
  UNSUPPORTED_SCHEMA_VERSION: { cause: '지원하지 않는 메타데이터 버전입니다', action: '최신 화면·에이전트를 사용하세요' },
  RATE_LIMITED: { cause: '요청이 너무 많습니다', action: '잠시 뒤 다시 시도하세요' },
  STORAGE_UNAVAILABLE: { cause: '저장소를 일시적으로 쓸 수 없습니다', action: '잠시 뒤 재시도하세요. 계속되면 운영기관에 문의하세요' },
  SERVICE_UNAVAILABLE: { cause: '서비스 점검 중이거나 일시 장애입니다', action: '잠시 뒤 재시도하세요' },
  INTERNAL: { cause: '서버 오류가 발생했습니다', action: '요청 ID와 함께 운영기관에 문의하세요' },
}

export function explain(code: string | undefined): { cause: string; action: string } {
  return PROBLEM_HELP[code ?? ''] ?? { cause: '알 수 없는 오류입니다', action: '요청 ID와 함께 운영기관에 문의하세요' }
}
