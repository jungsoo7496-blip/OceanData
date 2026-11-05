"""contracts/openapi.yaml 기준 자동 계약 시험 — Schemathesis (04번 요구사항 10.2).

계약 파일에서 요청을 생성해 앱(ASGI, 프로세스 안)에 보내고 응답을 계약과 대조한다.
- 양성(positive): 계약에 맞는 입력 → 5xx 없음, 문서화된 상태 코드·Content-Type·응답 스키마·헤더, 인증 없이 보내면 401
- 음성(negative): 계약을 어긴 입력 → 5xx 없음, 4xx로 거절, 문서화된 상태 코드·응답 스키마

범위 (명시 목록): 04번 수집 흐름(업로드·자산·장비 정책·에이전트 상태)과 그 흐름이 쓰는 01~03번 조회 API.
API 전체(125개 작업)를 돌리면 시간이 길고, 다른 스펙의 작업은 그 스펙의 계약 시험이 맡는다 (AGENTS.md 2).
쓰기 작업은 업로드 계열만, 없는 ID·계약 위반 입력으로만 보낸다 (정상 업로드는 examples/python 예제 클라이언트 시험).
결정적으로 돌린다: Hypothesis derandomize, 작업마다 예제 MAX_EXAMPLES개, 축소(shrink) 단계 없음.

알려진 계약 불일치 (M1~M8)는 아래 KNOWN_* 표에서 그 검사만 봐주고, test_known_contract_mismatch가 strict xfail로
재현한다. 앱이나 계약이 고쳐지면 xfail이 XPASS로 실패하므로 표의 해당 줄과 xfail 항목을 함께 지운다.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import wraps
from typing import Any

import pytest
import schemathesis
from hypothesis import HealthCheck, Phase, given, settings
from hypothesis import strategies as st
from schemathesis import GenerationMode
from schemathesis.checks import CHECKS, load_all_checks

from app.ratelimit import Limiter
from app.seed import DEV_A_CAMERA, DEV_A_COLLECTOR, SES_A
from app.services.contracts import contracts_dir
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

# ---- 알려진 계약 불일치 (보고 항목 번호). 앱 코드·계약은 이 작업 범위 밖이라 고치지 않고 보고만 한다.
# (작업, 상태 코드) → 계약에 없는 상태 코드지만 status_code_conformance에서 봐준다
KNOWN_UNDOCUMENTED_STATUS: dict[tuple[str, int], str] = {
    ("GET /v1/projects", 400): "M1", ("GET /v1/sessions", 400): "M1", ("GET /v1/devices", 400): "M1",
    ("GET /v1/uploads/{upload_id}/parts", 409): "M2",
    ("POST /v1/uploads", 404): "M5",
}
# 계약을 어긴 조회 조건을 2xx로 받아 주는 작업 → negative_data_rejection에서 봐준다
KNOWN_ACCEPTS_INVALID_QUERY: dict[str, str] = {"GET /v1/assets": "M4", "GET /v1/sessions": "M4"}
# 계약에 상한이 없는 값을 서버가 상한으로 400 거절하는 본문 위치 → positive_data_acceptance에서 봐준다
KNOWN_UNDECLARED_LIMITS: dict[str, tuple[str, str]] = {
    "POST /v1/uploads/{upload_id}/complete": (r"/parts(/\d+/(part_number|etag))?", "M7"),
}
# M3: 문제 응답의 instance에 디코딩한 경로를 그대로 넣어 비ASCII·제어 문자 경로면 uri-reference가 아니다
#     → 경로 값은 URI 경로 조각에 쓸 수 있는 문자만 생성한다 (_uri_safe_path)
# M6: 모든 인증 작업이 429를 낼 수 있지만 대부분 계약에 없다 → 호출 제한을 넉넉히 올려 이 시험에서는 429가 나지 않게 한다
# M8: 문자열 경로·조회 값에 NUL이 있으면 500 → 생성하지 않는다 (_generatable)


def _tolerating(check: Callable[..., Any], skip: Callable[[Any, Any], bool]) -> Callable[..., Any]:
    @wraps(check)
    def wrapper(ctx: Any, response: Any, case: Any) -> Any:
        if skip(response, case):
            return None
        return check(ctx, response, case)

    return wrapper


def _checks(names: tuple[str, ...]) -> list[Callable[..., Any]]:
    load_all_checks()
    out = []
    for name, fn in zip(names, CHECKS.get_by_names(list(names)), strict=True):
        if name == "status_code_conformance":
            fn = _tolerating(fn, lambda r, c: (c.operation.label, r.status_code) in KNOWN_UNDOCUMENTED_STATUS)
        elif name == "negative_data_rejection":
            fn = _tolerating(fn, lambda r, c: c.operation.label in KNOWN_ACCEPTS_INVALID_QUERY
                             and 200 <= r.status_code < 300)
        elif name == "positive_data_acceptance":
            fn = _tolerating(fn, _undeclared_limit_rejection)
        out.append(fn)
    return out


def _undeclared_limit_rejection(response: Any, case: Any) -> bool:
    known = KNOWN_UNDECLARED_LIMITS.get(case.operation.label)
    if known is None or response.status_code != 400:
        return False
    errors = response.json().get("errors") or []
    return bool(errors) and all(re.fullmatch(known[0], e.get("pointer", "")) for e in errors)


_URI_SEGMENT = re.compile(r"[A-Za-z0-9\-._~!$&'()*+,;=:@]*")


def _uri_safe_path(case: Any) -> bool:
    # M3: 서버가 디코딩한 경로를 instance에 그대로 넣으므로 URI 경로 조각에 그대로 쓸 수 있는 문자만 생성한다
    return all(isinstance(v, str) and _URI_SEGMENT.fullmatch(v) for v in (case.path_parameters or {}).values())


def _generatable(case: Any) -> bool:
    query = case.query or {}
    # cursor는 서버가 발급한 불투명 토큰이다. 임의 문자열은 계약 형식(문자열 ≤512)에 맞아도 의미가 없다
    if "cursor" in query:
        return False
    # M8: NUL(\x00)이 든 문자열 조건은 DB까지 가서 500이 난다 → 생성하지 않는다 (xfail로 따로 재현)
    return not any("\x00" in str(v) for v in query.values()) and _uri_safe_path(case)


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
    _set_rate_limit(app, 100_000)  # M6 참고. 호출 제한 자체는 별도 시험(04번 요구사항 9)
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


# ---- 알려진 불일치 재현 (strict xfail). 고쳐지면 XPASS → 위 KNOWN_* 표와 이 항목을 지운다.
# 전제(현재 동작)가 바뀌면 AssertionError로 그냥 실패한다. xfail로 넘기는 것은 ContractMismatch뿐이다.
class ContractMismatch(Exception):
    pass


def _require_documented(env: ContractEnv, label: str, status: int) -> None:
    documented = set(env.operation(label).responses.status_codes)
    if str(status) not in documented:
        raise ContractMismatch(f"{label}: {status} 응답이 계약에 없다 (계약: {sorted(documented)})")


def _m1(env: ContractEnv) -> None:
    for path in ("/v1/projects", "/v1/sessions", "/v1/devices"):
        assert env.client.get(f"{path}?limit=0", headers=env.headers).status_code == 400
        assert env.client.get(f"{path}?cursor=not-a-cursor", headers=env.headers).status_code == 400
        _require_documented(env, f"GET {path}", 400)


def _m2(env: ContractEnv) -> None:
    done = env.seeded["upload_id"][0]
    r = env.client.get(f"/v1/uploads/{done}/parts", headers=env.headers)
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"
    _require_documented(env, "GET /v1/uploads/{upload_id}/parts", 409)


def _m3(env: ContractEnv) -> None:
    r = env.client.get("/v1/uploads/%C3%BE", headers=env.headers)
    assert r.status_code == 404 and r.json()["instance"] == "/v1/uploads/\u00fe"
    try:
        env.operation("GET /v1/uploads/{upload_id}").validate_response(r)  # instance: format uri-reference
    except Exception as exc:  # noqa: BLE001 - Schemathesis FailureGroup
        raise ContractMismatch(str(exc)) from None


def _m4(env: ContractEnv) -> None:
    for path in ("/v1/assets?data_type=NOT_A_DATA_TYPE", "/v1/assets?source_system_id=x",
                 "/v1/assets?captured_from=2026-11-01T09:00:00%2B09:00", "/v1/sessions?project_id=x",
                 "/v1/sessions?label=" + "x" * 121):
        r = env.client.get(path, headers=env.headers)
        assert r.status_code in (200, 400), r.text
        if r.status_code != 400:
            raise ContractMismatch(f"GET {path}: 계약을 어긴 조회 조건을 {r.status_code}로 받았다 (400이어야 함)")


def _m5(env: ContractEnv) -> None:
    r = env.client.post("/v1/uploads", json=manifest(b"synthetic", source="dev_" + "Z" * 26), headers=env.headers)
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"
    _require_documented(env, "POST /v1/uploads", 404)


def _m6(env: ContractEnv) -> None:
    _set_rate_limit(env.app, 1)
    env.client.get("/v1/me", headers=env.headers)
    r = env.client.get("/v1/me", headers=env.headers)
    assert r.status_code == 429 and r.headers.get("retry-after")
    _require_documented(env, "GET /v1/me", 429)


def _m7(env: ContractEnv) -> None:
    pending = env.seeded["upload_id"][1]
    r = env.client.post(f"/v1/uploads/{pending}/complete", json={"parts": [{"part_number": 10001, "etag": "x"}]},
                        headers=env.headers)
    assert r.status_code == 400 and r.json()["errors"][0]["pointer"] == "/parts/0/part_number", r.text
    item = env.schema.raw_schema["components"]["schemas"]["CompleteRequest"]["properties"]["parts"]["items"]
    maximum = item["properties"]["part_number"].get("maximum")
    if maximum is None or maximum >= 10001:
        raise ContractMismatch(f"계약 CompleteRequest.part_number 최댓값 {maximum} — 서버는 10000을 넘으면 400")


def _m8(env: ContractEnv) -> None:
    for path in ("/v1/sessions?label=%00", "/v1/assets?data_type=%00", "/v1/uploads/%00", "/v1/assets/ast_%00"):
        r = env.client.get(path, headers=env.headers)
        assert r.status_code in (400, 404, 500), r.text
        if r.status_code >= 500:
            body = r.json()
            raise ContractMismatch(f"GET {path}: {r.status_code} {body.get('code')} "
                                   f"(request_id={body.get('request_id')!r})")


KNOWN_MISMATCHES: dict[str, tuple[Callable[[ContractEnv], None], str]] = {
    "M1": (_m1, "목록 조회(listProjects·listSessions·listDevices)가 잘못된 limit·cursor에 400을 내지만 계약에 400이 없다"),
    "M2": (_m2, "완료된 업로드의 GET /v1/uploads/{id}/parts가 409 CONFLICT를 내지만 계약에 409가 없다"),
    "M3": (_m3, "problem+json instance에 디코딩한 경로를 넣어 비ASCII·제어 문자 경로면 uri-reference 형식이 아니다"),
    "M4": (_m4, "GET /v1/assets·/v1/sessions가 계약 밖 조회 조건(enum·pattern·maxLength·UTC)을 400 없이 200으로 받는다"),
    "M5": (_m5, "POST /v1/uploads가 없는·범위 밖 장비/회차에 404를 내지만 계약 createUpload에 404가 없다"),
    "M6": (_m6, "호출 제한 429가 모든 인증 작업에서 나오지만 대부분 작업의 계약 응답에 429가 없다"),
    "M7": (_m7, "계약 CompleteRequest에 part_number·etag·parts 상한이 없는데 서버는 상한(10000·200자·10000개)으로 400을 낸다"),
    "M8": (_m8, "문자열 경로·조회 값에 NUL(%00)이 있으면 DB 오류가 그대로 500 INTERNAL이 된다 (request_id도 빈 문자열)"),
}


@pytest.mark.parametrize("mid", [
    pytest.param(k, marks=pytest.mark.xfail(strict=True, raises=ContractMismatch, reason=f"{k}: {v[1]}"))
    for k, v in KNOWN_MISMATCHES.items()])
def test_known_contract_mismatch(contract_env: ContractEnv, mid: str) -> None:
    KNOWN_MISMATCHES[mid][0](contract_env)
