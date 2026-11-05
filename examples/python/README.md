# Python 예제 클라이언트 — 업로드·재개·완료

`contracts/openapi.yaml`의 업로드 흐름을 그대로 따르는 작은 클라이언트다 (04번 요구사항 10.2).
외부 기관 서버가 연동할 때 참고하는 용도이며, 같은 코드를 `backend/tests/contract/test_example_client.py`가
계약 시험으로 돌린다 (끊김 → 재개 → 완료 → 영수증, 받은 응답은 모두 계약과 대조).

| 단계 | API | 메모 |
|---|---|---|
| 토큰 | `POST /v1/auth/token` (client_credentials) | API 키·기기 자격증명일 때만. 사용자 토큰은 그대로 쓴다 |
| 업로드 생성 | `POST /v1/uploads` + `Idempotency-Key` | 매니페스트 신고 → upload_id, 파트 크기·수, 파트별 사전 서명 URL |
| 파트 전송 | 사전 서명 URL로 `PUT` | 스토리지로 직접 보낸다. API 서버를 거치지 않고 API 토큰도 붙이지 않는다 |
| 재개 | `GET /v1/uploads/{id}` → `GET /v1/uploads/{id}/parts` | 진행 중이면 완료된 파트는 건너뛰고 남은 파트의 새 URL로 보낸다 |
| 완료 | `POST /v1/uploads/{id}/complete` | 202, `ingest_status=RECEIVED`. 수신은 등록이 아니다 |
| 결과 | `GET /v1/uploads/{id}`, `GET /v1/assets/{id}/receipt` | 무결성 검증 뒤 `REGISTERED`(이후 단계 포함) 또는 `QUARANTINED` |

의존성은 httpx 하나다 (`pip install -r examples/python/requirements.txt`). Python 3.12 기준.

## 인증 (비밀값은 환경변수로만)

| 환경변수 | 뜻 |
|---|---|
| `MARINE_API_URL` | API 주소 (기본 `http://localhost:8000`) |
| `MARINE_TOKEN` | 사용자(Keycloak) 또는 이미 받은 액세스 토큰. 만료되면 다시 받아 넣어야 한다 |
| `MARINE_CLIENT_ID`, `MARINE_CLIENT_SECRET` | API 키(`key_…`) 또는 기기 자격증명(`dcr_…`). 단기 토큰을 받아 쓰고 만료되면 다시 받는다 |

둘 중 하나가 있어야 한다. 토큰·비밀은 코드·파일·로그에 남기지 않는다. 재개용 상태 파일에는 upload_id·asset_id·sha256만 적는다.
API 키는 기관 관리자가 `POST /v1/api-keys`로 발급한다 (범위: `upload:write`, `status:read`, `asset:read`).

## 개발 스택(`make up`)에서 돌리기

시드 값(`backend/app/seed.py`): 제공기관 A의 선수 카메라 `dev_00000000000000000000000302`(C2 — `CLIP`·`PROXY`·`SAMPLE_FRAME`만 허용),
수질 센서 `dev_00000000000000000000000303`, 1차 항해 회차 `ses_00000000000000000000000401`.

```bash
cp .env.example .env            # 처음 한 번 (값은 개발용)
make up && make seed            # API·워커·SeaweedFS·Keycloak 기동, 시험용 기관·사용자·장비·회차 생성
pip install -r examples/python/requirements.txt

# 구성원 계정(member-a) 개발 토큰. 개발 비밀번호는 deploy/compose/keycloak/README.md 참고 (운영 realm에는 없음)
export MARINE_API_URL=http://localhost:8000
export MARINE_TOKEN="$(curl -s -d grant_type=password -d client_id=marine-dev-cli -d username=member-a \
  -d password="$DEV_PASSWORD" http://localhost:8080/realms/marine/protocol/openid-connect/token \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')"

# 합성 영상 (ffmpeg testsrc2, 사람 없음)
make sample-video
python3 examples/python/marine_client.py upload var/samples/with_capture_time.mp4 \
  --source dev_00000000000000000000000302 --session ses_00000000000000000000000401 \
  --data-type VIDEO --variant CLIP --mime video/mp4 \
  --capture-start 2026-11-01T01:02:03Z --time-source DEVICE --wait 120
```

`--wait`을 주면 워커의 무결성 검증이 끝날 때까지 기다렸다가 영수증까지 출력한다. 출력은 JSON이다.
합성 센서 CSV는 `--source dev_00000000000000000000000303 --data-type SENSOR --variant ORIGINAL --mime text/csv`로 올린다.

```bash
python3 examples/python/marine_client.py status upl_…      # 업로드 상태 (RECEIVED와 REGISTERED를 구분)
python3 examples/python/marine_client.py receipt ast_…     # 등록 영수증
```

## 끊겼을 때 (재개)

같은 명령을 다시 실행하면 된다.

1. 업로드를 만들면 `<파일>.marine-upload.json`(또는 `--state`)에 upload_id를 적는다.
2. 다시 실행하면 `GET /v1/uploads/{id}`로 아직 진행 중인지 보고, `GET /v1/uploads/{id}/parts`가 준 완료 파트는 건너뛰고
   남은 파트만 새 URL로 보낸다. 이미 완료된 세션이면 아무것도 보내지 않는다.
3. 상태 파일이 없어도 24시간 안이면 같은 `Idempotency-Key`(장비 ID + sha256으로 만든다)에 서버가 처음 응답(같은 upload_id)을
   다시 주므로 똑같이 이어 간다. 이미 등록된 파일을 다른 키로 보내면 `200 ALREADY_REGISTERED`가 온다.
4. 세션 유효 기간(기본 7일)이 지나면 `410 GONE` — 상태 파일을 지우고 새 세션으로 처음부터 보낸다.

`429`·`503`에는 `Retry-After`만큼 기다렸다 다시 보낸다. 사전 서명 URL이 만료되면(`403`) 남은 파트 URL을 다시 받는다.
파트는 순서대로 하나씩 보낸다 (예제라 단순하게 둔다. 동시 전송은 에이전트(06번)가 정책의 `max_parallel_parts`로 한다).

## 라이브러리로 쓰기

`examples/python`을 `PYTHONPATH`에 두거나 파일을 복사해 쓴다.

```python
from pathlib import Path
from marine_client import MarineClient, build_manifest

path = Path("cam01_0001.mp4")
manifest = build_manifest(path, source_system_id="dev_…", session_id="ses_…", data_type="VIDEO",
                          variant="CLIP", mime_type="video/mp4")
with MarineClient.from_env() as client:
    result = client.upload_file(path, manifest, state_path=path.with_suffix(".marine-upload.json"))
    status = client.wait_until_settled(result.upload_id)
    if status["ingest_status"] == "QUARANTINED":
        print("격리:", status["quarantine_reason"])
```

오류는 `ApiError`로 올라오며 `code`(RFC 9457 problem+json의 `code`)로 분기한다. 예: `RULE_VIOLATION`(장비 정책이 그 variant를
허용하지 않음, `problem["allowed_variants"]`), `QUOTA_EXCEEDED`, `IDEMPOTENCY_CONFLICT`, `GONE`.

## 시험

```bash
cd backend
TEST_DATABASE_URL=postgresql+pg8000://marine_owner:change-me-owner@127.0.0.1:5432/marine \
  .venv/bin/pytest -q tests/contract/test_example_client.py tests/contract/test_schemathesis.py
```

## 확인 필요

- 개발 Keycloak 토큰은 아직 MFA로 인정되지 않아(`deploy/compose/keycloak/README.md`) 개발 스택에서 API 키 발급이
  `403 MFA_REQUIRED`가 된다. 그동안 개발 스택에서는 구성원 토큰(`MARINE_TOKEN`)으로 돌린다. 사용자 토큰은 이 예제가
  다시 받지 못하므로, 전송 중 만료되면 새 토큰으로 같은 명령을 다시 실행해 이어 올린다 (무인 연동은 API 키 방식).
