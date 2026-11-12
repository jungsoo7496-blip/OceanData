# ADR-0053: 요청 트랜잭션은 응답 전에 확정하고, 같은 Idempotency-Key 요청은 줄 세운다

- 상태: accepted
- 날짜: 2026-11-12
- 관련: 04번 요구사항 2.2·2.3, domain.md 7장, api-conventions.md, ADR-0005, 검토 2 #4(`docs/review-2026-11-05/README.md`), 계약 점검 14·20번

## 맥락

모든 API 경로는 `get_db`(yield 의존성)로 DB 세션을 받고, 세션의 커밋을 그 마무리 단계에서 한다. FastAPI는 0.118부터 yield 의존성의 기본 범위(`request`)를
**응답을 클라이언트에 보낸 뒤** 마무리한다(설치된 0.162.0 소스로 확인: `request` 범위의 AsyncExitStack은 `await response(...)` 뒤에 닫힌다).
그래서 두 가지가 어긋났다.

- 커밋이 실패해도(고유 제약·교착·연결 끊김) 클라이언트는 이미 2xx를 받았고 작업은 남지 않았다. `POST /v1/events`가 201과 `event_id`를 돌려줬는데 그 ID를 GET하면 404였다.
- 같은 `Idempotency-Key` 두 요청이 동시에 들어오면 둘 다 기록이 없다고 보고 일을 한 뒤 201을 보냈다. 뒤 요청의 커밋이 고유 키에서 깨지며 되돌려졌고, 그 요청은 이미 성공으로 응답한 뒤였다.
  `POST /v1/devices`·`POST /v1/sessions`도 같은 경로(`run_idempotent`)라 같은 문제였다(계약 점검 14·20번).

구간 클립 등록만 이 때문에 경로 안에서 직접 `db.commit()`을 했다(ADR-0033).

## 결정

1. **DB 세션을 `scope="function"`으로 받는다.** `deps.py`에 `DB = Depends(get_db, scope="function")`를 두고 모든 경로가 `db: Session = DB`로 받는다(21개 파일 151곳).
   FastAPI는 function 범위 yield 의존성을 경로 함수가 끝난 **직후, 응답을 보내기 전에** 마무리한다. 커밋이 실패하면 예외가 예외 처리기로 가서 문제 응답(problem+json)이 되고 클라이언트는 2xx를 받지 못한다.
   - 의존성 캐시 키에 범위가 들어가므로 한 곳이라도 `Depends(get_db)`(기본 범위)로 받으면 같은 요청 안에서 세션이 둘로 갈린다.
     그래서 `tests/unit/test_db_dependency_scope.py`가 모든 라우터의 모든 경로(중첩 의존성 포함)를 훑어 `get_db`가 function 범위인지 확인한다 — 새 경로가 빠뜨리면 시험이 실패한다.
   - 구간 클립 등록의 직접 커밋은 필요 없어져 뺐다.
2. **커밋 때 드러나는 동시 요청 충돌은 409로 돌려준다.** `errors.py`가 PostgreSQL SQLSTATE 23505(고유)·23503(외래 키)·23P01(배타) 위반을 409 `CONFLICT`로,
   40001(직렬화 실패)·40P01(교착)을 `Retry-After: 1`을 붙인 409로 매핑한다. 전에는 응답 뒤에 일어나 아무도 못 봤고, 이제 클라이언트에 보이므로 의미 있는 코드가 필요하다.
   서버 버그로 난 제약 위반도 같은 매핑으로 재시도 가능한 충돌처럼 보이므로(에이전트는 CONFLICT를 백오프로 계속 재시도한다) 조용히 넘기지 않는다: 경고 로그 `db_conflict_mapped`(SQLSTATE·제약 이름·표·경로,
   바인딩 값이 든 DETAIL은 제외)와 지표 `db_conflict_responses_total{sqlstate}`를 남긴다 (검토 K4).
3. **같은 키 요청은 DB 어드바이저리 잠금으로 줄 세운다.** `run_idempotent`가 키가 있는 요청에서 먼저 `pg_advisory_xact_lock(hashtextextended('idem|기관|주체|메서드+경로|키', 0))`을 잡고 나서 기록을 찾는다.
   잠금은 트랜잭션이 끝날 때(커밋·롤백) 풀리고, 1번 덕에 그 커밋은 앞 요청의 응답 전이다. 따라서 뒤 요청은 앞 요청이 확정된 뒤 기록을 보고 **같은 응답을 다시 받는다**(`Idempotent-Replayed: true`, 일은 한 번만).
   앞 요청이 오류로 끝나면(롤백) 기록이 없으므로 뒤 요청이 새로 처리한다.
   - 앞선 같은 키 요청이 없으면 `pg_try_advisory_xact_lock` 한 문장으로 바로 진행한다. 있으면 **프로세스마다 `MAX_LOCK_WAITERS`(8)개 요청만** `LOCK_WAIT_MS`(2초, `SET LOCAL lock_timeout`)까지 기다리고,
     자리가 없거나 시간이 지나면 기다리지 않고 409 + `Retry-After: 1`("앞선 요청이 아직 처리 중")이다. 기다리는 요청은 스레드 하나와 DB 연결 하나를 쥐므로 재시도 폭주(같은 키 60건)가 스레드 풀(40)과
     연결 풀(30)을 다 써 `/healthz`까지 18초 늦추던 것을 막는다(검토 K1). 기다린 뒤에는 `lock_timeout`을 기본값으로 되돌린다. `/healthz`는 입출력이 없으므로 `async def`로 두어 스레드 풀이 가득 차도 답한다.
   - `POST /v1/codes/event-categories/subcodes`가 따로 잡던 같은 종류의 잠금은 이제 중복이라 뺐다.

버린 대안:
- *경로 함수 안에서 명시적으로 `db.commit()`*: 151곳을 일일이 고쳐야 하고 빠뜨리기 쉽다. 커스텀 `APIRoute` 클래스도 라우터 21개를 건드려야 하고 세션을 요청 상태로 넘기는 장치가 더 필요하다. FastAPI가 지원하는 `scope="function"`이 한 곳(표시 하나)으로 같은 효과를 낸다.
- *`INSERT … ON CONFLICT`로 키를 먼저 선점(자리표시 행)*: 응답이 정해진 뒤 행을 고치는 단계가 늘고, 만료 기록 삭제와 섞이면 복잡하다. 어드바이저리 잠금은 조회→일→기록 전체를 한 번에 감싼다.
- *뒤 요청을 바로 409로*: 에이전트의 재시도는 같은 응답을 받는 쪽이 요구사항 2.2에 맞다. 기다림에 상한을 둬서 409(곧 다시)로 바뀌는 경우만 남긴다.

## 결과

좋아지는 점: 클라이언트는 DB에 확정된 일에만 2xx를 받는다. 같은 키 동시 재시도가 일을 한 번만 하고 같은 응답을 받는다. 커밋 실패가 로그에만 남지 않고 응답으로 보인다.

감수할 점:
- 커밋이 응답 시간에 들어간다(전에는 응답 뒤에 숨어 있었다).
- 일이 DB 밖에 만든 것(S3 멀티파트, 별도 연결의 큐 등록·감사)은 커밋이 실패해도 되돌릴 수 없다. 재시도의 결과는 경로마다 다르다(이제 클라이언트가 실패를 안다):
  - `POST /v1/uploads`: 재시도가 새 멀티파트를 만들어 이전 것이 고아로 남는다(만료 정리는 기록된 세션만 찾는다).
  - `POST /v1/uploads/{id}/complete`: 저장소에서 멀티파트가 먼저 끝나는데 커밋이 실패하면 객체는 완성됐고 DB는 UPLOADING이다. 재시도는 저장소의 객체가 신고 크기로 이미 있는 것을 확인하고(`_completed_in_storage`)
    파트 목록을 다시 맞추지 않고 DB 전이(RECEIVED·무결성 검증 작업 등록)만 마친다 — 전에는 완료된 멀티파트의 파트 목록을 조회하지 못해 세션이 만료될 때까지 503만 받았다(검토 K3, `test_complete_after_commit_failure.py`).
  - 별도 연결의 큐 등록·감사는 커밋 실패 뒤에도 남고, 같은 키 재시도가 한 번 더 남긴다.
- 앞 요청이 2초 넘게 걸리거나 대기자가 이미 8개이면 같은 키의 뒤 요청은 409(`Retry-After: 1`)를 받는다. 잠금 순서: 어드바이저리 잠금은 `run_idempotent` 호출 시점(경로의 첫 DB 쓰기·행 잠금보다 앞)에 잡는다. 경로가 행 잠금을 먼저 잡고 이 잠금을 나중에 잡으면 교착이 날 수 있고, 그때는 PostgreSQL이 한쪽을 중단해 409(`Retry-After`)가 된다.
- 응답 본문을 만드는 동안 세션이 필요한 스트리밍 응답이 생기면 function 범위와 맞지 않는다. 지금 `backend/app`에는 `StreamingResponse`·SSE가 없고 보고서 내보내기·프레임은 `Response(bytes)`다. 생기면 그 경로만 request 범위 세션을 따로 쓴다.

FastAPI 하한은 0.121이다(`Depends(scope=…)`가 0.121부터 있고, 그 아래에서는 import 때 TypeError다). 의존성의 마무리 시점은 0.162에서 확인했고 `test_db_dependency_scope.py`가 조립된 앱의 모든 경로를 확인한다.

되돌리는 방법: `DB`의 `scope="function"`을 빼면 이전 동작(응답 뒤 커밋)이다. 어드바이저리 잠금은 `_serialize` 호출 한 줄로 끌 수 있다. 마이그레이션은 없다. 계약은 **설명 문구만** 바꿨다
(`Idempotency-Key` 매개변수 설명과 `Conflict` 응답의 `Retry-After`; 409는 이미 문서화돼 있다). 되돌릴 때는 이 문구도 함께 되돌린다 — 아니면 계약이 서버가 더는 하지 않는 대기를 약속한다.

시험: `tests/unit/test_db_dependency_scope.py`(모듈 라우터와 조립된 앱의 모든 경로의 범위, get_db 외 마무리 단계 의존성 없음), `tests/integration/test_commit_before_response.py`(커밋 실패가 2xx로 새지 않음,
커밋 때 고유 제약 위반 → 409와 롤백과 로그·지표, 같은 키 동시 요청 두 곳(`/v1/events`·`/v1/sessions`)이 일을 한 번만 하고 같은 응답, 기다림 상한 → 409, 대기자 상한 → 즉시 409, 오류로 끝난 요청이 키를 쥐지 않음,
확정된 기록은 바로 재생), `tests/integration/test_complete_after_commit_failure.py`(완료 뒤 커밋 실패의 재시도), `tests/unit/test_example_client_retry.py`(예제 클라이언트가 Retry-After가 붙은 409를 재시도).
수정 전 코드에서 이 중 6개가 실패하는 것을 확인했다(첫 구현). 독립 검토(K1~K7)에서 나온 보완은 각각 재현 시험을 먼저 썼다.
