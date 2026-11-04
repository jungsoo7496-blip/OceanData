# 09 토큰·비밀값 유출 의심

**판단 기준:** 비밀값 검사(`make secrets-check`·gitleaks) 경고, 알 수 없는 위치의 요청(감사 로그 source_ip), 비정상 다운로드 URL 발급 급증.

**자동 대응:** 사전 서명 URL은 짧게 만료(기본 5분), 로그에는 토큰·URL을 남기지 않는다(마스킹).

**사람이 할 것**
1. 해당 자격증명 즉시 폐기 (04 Runbook). 사용자 토큰이면 Keycloak에서 세션 종료·비밀번호 재설정.
2. 서명 키 의심: `DEVICE_TOKEN_SIGNING_KEY` 교체 → 재기동 (모든 기기 토큰 무효, 에이전트는 자격증명으로 새로 받음).
3. 감사 로그(`GET /v1/audit-logs`)로 유출 기간의 열람·다운로드 URL 발급(DOWNLOAD_URL_ISSUED·DISTRIBUTION_ISSUED) 범위 확인.
4. 개인정보 포함 가능성이 있으면 15.3 장애 대응 절차(통지)로.

**완료 확인:** 폐기·교체 후 기존 토큰 401, 감사 로그 체인 정상(`make verify-audit`), 영향 범위 보고서 작성.
