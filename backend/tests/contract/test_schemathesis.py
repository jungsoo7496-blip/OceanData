"""contracts/openapi.yaml 기준 자동 계약 시험 — Schemathesis (04번 요구사항 10.2).

계약 파일에서 요청을 생성해 앱(ASGI, 프로세스 안)에 보내고 응답을 계약과 대조한다.
- 양성(positive): 계약에 맞는 입력 → 5xx 없음, 문서화된 상태 코드·Content-Type·응답 스키마·헤더, 인증 없이 보내면 401
- 음성(negative): 계약을 어긴 입력 → 5xx 없음, 4xx로 거절, 문서화된 상태 코드·응답 스키마

범위 (명시 목록): 04번 수집 흐름(업로드·자산·장비 정책·에이전트 상태)과 그 흐름이 쓰는 01~03번 조회 API.
API 전체(125개 작업)를 돌리면 시간이 길고, 다른 스펙의 작업은 그 스펙의 계약 시험이 맡는다 (AGENTS.md 2).
쓰기 작업은 업로드 계열만, 없는 ID·계약 위반 입력으로만 보낸다 (정상 업로드는 examples/python 예제 클라이언트 시험).
결정적으로 돌린다: Hypothesis derandomize, 작업마다 예제 MAX_EXAMPLES개, 축소(shrink) 단계 없음.

처음 돌렸을 때 찾은 불일치 M1~M8(계약에 없는 400·404·409·429, 상한 없는 완료 요청, instance 인코딩, 계약을 어긴 조회 조건,
NUL 문자 500)은 계약·앱을 고쳤고, 봐주는 표 없이 모든 검사를 돌린다. test_fixed_mismatch_*가 각 항목의 회귀를 막는다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
import schemathesis
from hypothesis import HealthCheck, Phase, given, settings
from hypothesis import strategies as st
from schemathesis import GenerationMode
from schemathesis.checks import CHECKS, load_all_checks

from app.ratelimit import Limiter
from app.seed import DEV_A_CAMERA, DEV_A_COLLECTOR, PRJ_A, SES_A, sub
from app.services.contracts import contracts_dir
from tests.conftest import user_token
from tests.integration.test_ingest import drain, manifest, mp4_bytes, upload_all

pytestmark = pytest.mark.integration
MB = 1024 * 1024
MAX_EXAMPLES = 15

READ_OPERATIONS = (
    "GET /healthz", "GET /readyz", "GET /v1/system/capabilities", "GET /v1/me",
    "GET /v1/projects", "GET /v1/sessions", "GET /v1/sessions/{session_id}",
    "GET /v1/devices", "GET /v1/devices/{device_id}", "GET /v1/devices/{device_id}/policy",
    "GET /v1/agents/{device_id}/health",
    "GET /v1/schemas", "GET /v1/schemas/{name}/{version}", "GET /v1/codes/{table}",
    "GET /v1/uploads/{upload_id}", "GET /v1/uploads/{upload_id}/parts",
    "GET /v1/assets", "GET /v1/assets/{asset_id}", "GET /v1/assets/{asset_id}/receipt",
    "GET /v1/assets/{asset_id}/lineage",
)
# 양성 입력이라도 대상 ID는 생성값이라 없는 업로드 → 404 (계약에 있음). 업로드 생성(POST /v1/uploads)은 음성만.
UPLOAD_WRITE_OPERATIONS = ("POST /v1/uploads/{upload_id}/complete", "DELETE /v1/uploads/{upload_id}")
NEGATIVE_OPERATIONS = tuple(
    o for o in READ_OPERATIONS if "{" in o or o in ("GET /v1/projects", "GET /v1/sessions", "GET /v1/devices",
                                                    "GET /v1/assets")
) + ("POST /v1/uploads",) + UPLOAD_WRITE_OPERATIONS

POSITIVE_CHECKS = ("not_a_server_error", "status_code_conformance", "content_type_conformance",
                   "response_schema_conformance", "response_headers_conformance", "positive_data_acceptance",
                   "ignored_auth")
NEGATIVE_CHECKS = ("not_a_server_error", "status_code_conformance", "content_type_conformance",
                   "response_schema_conformance", "negative_data_rejection")

def _checks(names: tuple[str, ...]) -> list[Callable[..., Any]]:
    load_all_checks()
    return list(CHECKS.get_by_names(list(names)))


def _generatable(case: Any) -> bool:
    query = case.query or {}
    # cursor는 서버가 발급한 불투명 토큰이다. 임의 문자열은 계약 형식(문자열 ≤512)에 맞아도 의미가 없다
    return "cursor" not in query


def _set_rate_limit(app: Any, per_minute: int) -> None:
    # app/api/deps.py가 호출 제한기를 app.state.__dict__["limiter"]에 둔다 (첫 요청 때 설정값으로 만든다)
    app.state.__dict__["limiter"] = Limiter(per_minute)


@dataclass
class ContractEnv:
    schema: Any
    app: Any
    client: Any
    headers: dict[str, str]
    seeded: dict[str, list[str]]

    def operation(self, label: str) -> Any:
        method, path = label.split(" ", 1)
        return self.schema[path][method]


@pytest.fixture()
def contract_env(ictx) -> Iterator[ContractEnv]:
    """시드 DB(ictx: 기관 A·B, 장비, 회차) + 등록 완료 업로드 1건 + 진행 중 업로드 1건."""
    app = ictx.client.app
    _set_rate_limit(app, 100_000)  # 429는 계약에 있지만(M6) 이 시험은 실제 응답을 보려고 제한을 넉넉히 둔다
    h = ictx.as_user(102)  # 제공기관 A 구성원 (업로드·자산 권한)
    data = mp4_bytes(5 * MB, seed=501)
    done = upload_all(ictx, manifest(data), data, h)
    drain(ictx)
    pending = ictx.client.post("/v1/uploads", json=manifest(mp4_bytes(5 * MB, seed=502)), headers=h)
    assert pending.status_code == 201, pending.text
    schema = schemathesis.openapi.from_path(contracts_dir() / "openapi.yaml")
    schema.app = app
    token = h["Authorization"].removeprefix("Bearer ")

    # 생성 단계에서 토큰을 붙인다. 음성 입력이 인증 헤더를 빼는 경우(security 부정)에는 붙이지 않아 401을 확인한다
    @schema.auth(refresh_interval=None)
    class _UserToken:
        def get(self, case: Any, context: Any) -> str:
            return token

        def set(self, case: Any, data: str, context: Any) -> None:
            case.headers = {**(case.headers or {}), "Authorization": f"Bearer {data}"}

    yield ContractEnv(schema=schema, app=app, client=ictx.client, headers=h, seeded={
        "session_id": [SES_A], "device_id": [DEV_A_CAMERA, DEV_A_COLLECTOR],
        "upload_id": [done["upload_id"], pending.json()["upload_id"]], "asset_id": [done["asset_id"]],
        "name": ["upload-manifest"], "version": ["1.0.0"],
    })


def _run(env: ContractEnv, labels: tuple[str, ...], mode: GenerationMode, checks: list[Callable[..., Any]],
         *, use_seeded: bool) -> dict[str, set[int]]:
    failures: list[str] = []
    seen: dict[str, set[int]] = {label: set() for label in labels}
    for label in labels:
        strategy = env.operation(label).as_strategy(generation_mode=mode).filter(_generatable)

        @settings(max_examples=MAX_EXAMPLES, derandomize=True, deadline=None, database=None,
                  suppress_health_check=list(HealthCheck), phases=[Phase.explicit, Phase.generate])
        @given(case=strategy, pick=st.integers(min_value=0, max_value=3))
        def check_operation(case: Any, pick: int) -> None:
            # 양성 조회의 경로 ID는 4번 중 3번 시드된 실제 ID로 바꿔 200 응답도 계약과 대조한다 (나머지는 생성값 → 404)
            if use_seeded and pick and case.method == "GET" and case.path_parameters:
                for name, values in env.seeded.items():
                    if name in case.path_parameters:
                        case.path_parameters[name] = values[pick % len(values)]
            seen[label].add(case.call_and_validate(checks=checks).status_code)

        try:
            check_operation()
        except Exception as exc:  # noqa: BLE001 - 작업별 실패를 모아 한 번에 보고한다
            failures.append(f"### {label}\n{exc}")
    assert not failures, f"{len(failures)}개 작업이 계약과 다르다:\n\n" + "\n\n".join(failures)
    return seen


def test_schemathesis_positive(contract_env: ContractEnv) -> None:
    """계약에 맞는 요청: 5xx 없음, 문서화된 상태·Content-Type·응답 스키마·헤더, 인증 무시 없음."""
    seen = _run(contract_env, READ_OPERATIONS + UPLOAD_WRITE_OPERATIONS, GenerationMode.POSITIVE,
                _checks(POSITIVE_CHECKS), use_seeded=True)
    # 헛돌지 않았는지: 조회 작업마다 200 응답을 적어도 한 번 계약과 대조했다
    assert not [label for label in READ_OPERATIONS if 200 not in seen[label]], seen


def test_schemathesis_negative(contract_env: ContractEnv) -> None:
    """계약을 어긴 요청: 5xx 없이 4xx로 거절하고, 거절 응답도 계약(problem+json)과 맞다."""
    _run(contract_env, NEGATIVE_OPERATIONS, GenerationMode.NEGATIVE, _checks(NEGATIVE_CHECKS), use_seeded=False)


# ---- 고친 불일치의 회귀 시험 (처음 Schemathesis로 찾은 M1~M8)
def _documented(env: ContractEnv, label: str) -> set[str]:
    return set(env.operation(label).responses.status_codes)


def test_fixed_mismatch_status_codes_are_documented(contract_env: ContractEnv) -> None:
    env = contract_env
    for path in ("/v1/projects", "/v1/sessions", "/v1/devices"):  # M1: 잘못된 limit·cursor → 400
        assert env.client.get(f"{path}?limit=0", headers=env.headers).status_code == 400
        assert env.client.get(f"{path}?cursor=not-a-cursor", headers=env.headers).status_code == 400
        assert "400" in _documented(env, f"GET {path}")
    done = env.seeded["upload_id"][0]  # M2: 완료된 업로드의 파트 조회 → 409
    r = env.client.get(f"/v1/uploads/{done}/parts", headers=env.headers)
    assert r.status_code == 409 and "409" in _documented(env, "GET /v1/uploads/{upload_id}/parts")
    env.operation("GET /v1/uploads/{upload_id}/parts").validate_response(r)
    # M5: 없는·범위 밖 장비 → 404 (테넌트 격리)
    r = env.client.post("/v1/uploads", json=manifest(b"synthetic", source="dev_" + "Z" * 26), headers=env.headers)
    assert r.status_code == 404 and "404" in _documented(env, "POST /v1/uploads")
    # M6: 인증 작업은 모두 호출 제한(429)과 권한 거부(403: 역할·다중 인증·기기 범위)를 문서화한다 (전체 작업 탐색)
    spec = env.schema.raw_schema
    for code in ("429", "403"):
        missing = [f"{m.upper()} {p}" for p, item in spec["paths"].items() for m, op in item.items()
                   if isinstance(op, dict) and "responses" in op and "401" in op["responses"]
                   and code not in op["responses"]]
        assert not missing, (code, missing)
    _set_rate_limit(env.app, 1)
    try:
        env.client.get("/v1/me", headers=env.headers)
        r = env.client.get("/v1/me", headers=env.headers)
        assert r.status_code == 429 and r.headers.get("retry-after")
        env.operation("GET /v1/me").validate_response(r)
    finally:
        _set_rate_limit(env.app, 100_000)


def test_fixed_mismatch_complete_limits_match_server(contract_env: ContractEnv) -> None:
    env = contract_env  # M7: 계약 상한 = 서버 상한 (part_number ≤ 10000, etag ≤ 200자, parts ≤ 10000개)
    item = env.schema.raw_schema["components"]["schemas"]["CompleteRequest"]["properties"]["parts"]
    assert item["maxItems"] == 10000
    assert item["items"]["properties"]["part_number"]["maximum"] == 10000
    assert item["items"]["properties"]["etag"]["maxLength"] == 200
    pending = env.seeded["upload_id"][1]
    r = env.client.post(f"/v1/uploads/{pending}/complete", json={"parts": [{"part_number": 10001, "etag": "x"}]},
                        headers=env.headers)
    assert r.status_code == 400 and r.json()["errors"][0]["pointer"] == "/parts/0/part_number"


def test_fixed_mismatch_instance_query_and_nul(contract_env: ContractEnv) -> None:
    env = contract_env
    # M3: instance는 퍼센트 인코딩한 URI 참조
    r = env.client.get("/v1/uploads/%C3%BE", headers=env.headers)
    assert r.status_code == 404 and r.json()["instance"] == "/v1/uploads/%C3%BE"
    env.operation("GET /v1/uploads/{upload_id}").validate_response(r)
    # M4: 계약을 어긴 조회 조건 → 400 (열거값·ID 형식·UTC Z·길이), 맞는 조건은 그대로 200
    for path in ("/v1/assets?data_type=NOT_A_DATA_TYPE", "/v1/assets?source_system_id=x",
                 "/v1/assets?captured_from=2026-11-01T09:00:00%2B09:00", "/v1/sessions?project_id=x",
                 "/v1/sessions?label=" + "x" * 121):
        r = env.client.get(path, headers=env.headers)
        assert r.status_code == 400 and r.json()["errors"][0]["pointer"].startswith("/query/"), (path, r.text)
        env.operation("GET " + path.split("?")[0]).validate_response(r)
    assert env.client.get("/v1/assets?data_type=VIDEO&captured_from=2026-11-01T00:00:00Z",
                          headers=env.headers).status_code == 200
    op = {"Authorization": f"Bearer {user_token(sub(106), mfa=True)}"}  # 전체 탐색: 0000년은 날짜가 아니다 (500이었음)
    r = env.client.get("/v1/usage-reports?month=0000-01", headers=op)
    assert r.status_code == 400, r.text
    env.operation("GET /v1/usage-reports").validate_response(r)
    # M8: NUL은 조회 조건이면 400, 경로 ID면 404, JSON 본문 문자열이면 400 — 500이 아니고 요청 ID가 있다
    for path, status in (("/v1/sessions?label=%00", 400), ("/v1/assets?data_type=%00", 400),
                         ("/v1/assets?env_metric=a%00", 400), ("/v1/uploads/%00", 404), ("/v1/assets/ast_%00", 404),
                         ("/v1/sessions/%00", 404), ("/v1/devices/%00/policy", 404)):
        r = env.client.get(path, headers=env.headers)
        assert r.status_code == status, (path, r.status_code, r.text)
        assert r.json()["request_id"] == r.headers["x-request-id"]
    r = env.client.post("/v1/sessions", headers=env.headers, json={
        "project_id": PRJ_A, "label": "nul-check", "title": "제목\u0000",
        "starts_at": "2026-11-01T00:00:00Z"})
    assert r.status_code == 400 and r.json()["errors"][0]["code"] == "invalid_character", r.text


def test_fixed_full_exploration_findings(contract_env: ContractEnv) -> None:
    """전체 작업 탐색(test_schemathesis_all.py)으로 찾아 고친 것: 정수 범위 500, 1000년 이전 연도 출력, 멱등 키 길이,
    엔드포인트가 선언하지 않은 정수 조회 조건."""
    from datetime import UTC, datetime

    from app.timefmt import iso

    env = contract_env
    r = env.client.get("/v1/integration-cards/icd_S0SQEX477AB66ETWJDRJH5PMTK?version=5270030422", headers=env.headers)
    assert r.status_code == 400, r.text  # 32비트 정수를 넘는 값 (전에는 DB 오류 500)
    env.operation("GET /v1/integration-cards/{integration_card_id}").validate_response(r)
    assert iso(datetime(63, 5, 29, 0, 34, 56, tzinfo=UTC)) == "0063-05-29T00:34:56Z"  # 전에는 "63-05-29…"
    body = {"project_id": PRJ_A, "label": "idem-check", "title": "멱등 키 길이", "starts_at": "2026-11-01T00:00:00Z"}
    for key in ("short", "k" * 129):
        r = env.client.post("/v1/sessions", json=body, headers={**env.headers, "Idempotency-Key": key})
        assert r.status_code == 400 and r.json()["errors"][0]["pointer"] == "/header/idempotency-key", r.text
    r = env.client.post("/v1/sessions", json=body, headers={**env.headers, "Idempotency-Key": "k" * 8})
    assert r.status_code == 201, r.text
    op = {"Authorization": f"Bearer {user_token(sub(106), mfa=True)}"}  # 보고서 양식 구역의 명시적 null은 계약 밖
    r = env.client.post("/v1/report-templates", headers=op, json={
        "code": "NULLCHK", "name": "null 검사", "sections": [{"kind": "TEXT", "title": "본문", "metrics": None}]})
    assert r.status_code == 400 and r.json()["errors"][0]["pointer"] == "/sections/0/metrics", r.text
    for interval in ("PT95519096848682913101224M", "PT600000M", "PT9000H"):  # 깊은 탐색: 거대한 간격이 DB 범위를 넘어 500
        r = env.client.get(f"/v1/observations?metric=sea_water_temperature&interval={interval}"
                           "&from=2026-11-01T00:00:00Z&to=2026-11-02T00:00:00Z", headers=env.headers)
        assert r.status_code == 400 and r.json()["errors"][0]["pointer"] == "/query/interval", (interval, r.text)
    for path in ("/v1/datasets?limit=abc", "/v1/datasets?limit=0", "/v1/sessions?limit=5.0"):
        r = env.client.get(path, headers=env.headers)
        assert r.status_code in (400, 403), (path, r.text)
        if r.status_code == 400:
            assert r.json()["errors"][0]["pointer"] == "/query/limit", r.text
