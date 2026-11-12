# 05 긴급 열람

> 02번 요구사항 8 구현(ADR-0029). 사후 검토 양식·법무 승인 절차는 운영기관이 정한다(확인 필요).

**판단 기준:** 장애·사고 대응으로 운영자가 기관 원본을 직접 봐야 할 때만. 메타데이터·처리 상태로 판단할 수 있으면 열지 않는다.

**자동 대응:** 승인되면 대상 기관 ORG_ADMIN에게 메일(`break_glass.granted`). 원본 URL 발급마다 `BREAK_GLASS_ACCESS` 감사.
기간이 끝나면 접근은 즉시 막히고, 워커가 상태를 EXPIRED로 바꾸며 `BREAK_GLASS_EXPIRED`를 남긴다.

**사람이 할 것**
1. 요청(다중 인증): `POST /v1/break-glass-grants` — 대상(ASSET 또는 SESSION)·구체적 사유(장애 번호)·기간(최대 24시간, 필요한 만큼만).
2. 다른 운영자가 사유를 확인하고 승인(`POST /v1/break-glass-grants/{id}/decision`, APPROVE/DENY). 요청자 본인 승인은
   `BREAK_GLASS_SINGLE_APPROVAL=true`일 때만(1인 운영 기간).
3. 요청한 운영자만 `POST /v1/assets/{id}/download-url`로 받는다. 일이 끝나면 바로 `POST /v1/break-glass-grants/{id}/revoke`.
4. 사후: `GET /v1/break-glass-grants`와 감사 로그(`BREAK_GLASS_*`)를 기관과 공유한다. 기관 ORG_ADMIN도 같은 목록을 본다.

**완료 확인:** 요청·승인·접근·종료(만료 또는 회수)가 모두 감사 로그에 있고, 기관 통지 메일이 발송됐다.
