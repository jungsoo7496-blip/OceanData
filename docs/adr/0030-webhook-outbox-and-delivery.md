# ADR-0030: 기관 웹훅 — ORM outbox 발행, 저장하지 않는 서명 비밀, SSRF 차단, N회 실패 시 비활성

- 상태: accepted
- 날짜: 2026-11-05
- 관련: 04번 요구사항 8, contracts `webhooks` 절·webhook-event.schema.json(G1), security.md, ADR-0014(작업 큐), ADR-0019(before_flush 가드)

## 맥락
계약에는 구독 경로와 다섯 이벤트(upload.registered·upload.quarantined·asset.processed·share.status_changed·grant.revoked)가
G1로 있었지만 구현이 없었다. 상태를 바꾸는 코드 경로가 여럿이라(무결성 검증·재검증·파이프라인·공유 심사·데이터셋 철회 등)
경로마다 발행 코드를 넣으면 빠지기 쉽다. 구독자는 임의 URL을 주므로 서버가 내부망을 호출하는 SSRF 위험이 있다.

## 결정
1. **발행은 ORM `before_flush` outbox**: 자산 `ingest_status`·`share_status`, 이용 권한 `status`의 변경 이력을 보고 같은 트랜잭션에
   `webhook_deliveries` 행을 만든다(마이그레이션 0015). 상태 변경과 발행이 함께 커밋·롤백된다. 새로 만든 행(파생본 생성 등)은 대상이 아니다.
   - VALIDATING 등 → REGISTERED 이후 상태: `upload.registered`(업로드가 있는 자산만). 같은 플러시에서 READY_PRIVATE까지 가면 `asset.processed`도.
   - → QUARANTINED: `upload.quarantined`. share_status 변경: `share.status_changed`. 이용 권한 → REVOKED: `grant.revoked`(권한을 받은 이용기관에게).
2. **전송은 워커 주기**(`WEBHOOK_DISPATCH_INTERVAL_S`, SKIP LOCKED). 본문은 webhook-event 계약, 헤더 `X-Signature = hex(HMAC-SHA256(secret,
   timestamp + "." + body))`·`X-Signature-Timestamp`·`X-Event-Id`. 리디렉션은 따르지 않는다. 2xx만 성공.
3. **재시도**: `WEBHOOK_BACKOFF_BASE_S × 2^(n-1)`(상한 `WEBHOOK_BACKOFF_MAX_S`), `WEBHOOK_MAX_ATTEMPTS`번 실패하면 구독을 끄고
   남은 전송을 취소하고 기관 ORG_ADMIN에게 메일(URL 전체·비밀 없이 호스트만). 다시 쓰려면 새로 구독한다.
4. **서명 비밀은 저장하지 않는다**: 구독마다 무작위 salt만 저장하고 `HMAC(WEBHOOK_SECRET_KEY, salt)`로 다시 만든다(등록 응답에서 한 번만 보임).
   키가 없으면 `DEVICE_TOKEN_SIGNING_KEY`에서 용도 라벨로 파생한다. DB만 유출돼서는 서명을 위조할 수 없다. 키를 바꾸면 모든 구독 비밀이 바뀐다.
5. **SSRF**: https만, URL 사용자 정보 금지, 등록 때 IP 리터럴·localhost류를 거부하고 전송 직전에 이름을 풀어 사설·루프백·링크로컬·예약
   주소면 보내지 않는다(실패로 센다). 개발용 `WEBHOOK_ALLOW_PRIVATE_TARGETS`만 예외.

버린 대안: 작업 큐(enqueue)로 전송 — 업무 트랜잭션 커밋 전에 워커가 가져가면 행이 안 보여 이벤트가 사라질 수 있다. 비밀 암호화 저장 — 새 의존성(암호 라이브러리)과 키 관리가 같은 문제라 이점이 작다.

## 결과
- 좋아지는 점: 어떤 경로로 상태가 바뀌어도 웹훅이 빠지지 않고, 업무 롤백 시 유령 이벤트가 없다.
- 감수할 점: DNS 재바인딩(검사 후 연결 사이에 주소가 바뀌는 경우)은 완전히 막지 못한다 — 운영에서는 나가는 방화벽으로 사설 대역을
  막는다(확인 필요). 전송 중에는 행 잠금을 잡는다(짧은 시간 제한으로 완화). 꺼진 동안의 이벤트는 다시 보내지 않는다.
- 되돌리는 방법: 0015 down, 리스너·경로 제거.

## 갱신 (2026-11-05, 보안 검토 후속 — ADR-0041)
결정 2(전송)와 5(SSRF)의 방식, 감수할 점의 "전송 중 행 잠금"·"DNS 재바인딩"은 ADR-0041로 바뀌었다. 전송은 워커의 전용 스레드가
잠금 없이(가져오기·기록만 짧은 트랜잭션) 한 건 전체 기한 안에 보내고, 검사한 주소로만 연결한다. 공인(global)이 아닌 주소는 모두
막고, 기관당 구독 수 상한(넘으면 409)이 생겼다. 발행(outbox)·서명·재시도·비활성 규칙은 이 ADR 그대로다.
