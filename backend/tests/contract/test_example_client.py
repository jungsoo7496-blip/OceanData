"""examples/python 예제 클라이언트 계약 시험 (04번 요구사항 10.2): 업로드 → 끊김 → 재개 → 완료 → 영수증.

예제 클라이언트를 그대로 불러 프로세스 안의 앱(TestClient)과 moto S3에 붙인다. 예제가 받은 API 응답은 모두
contracts/openapi.yaml과 대조하고(응답 훅), 스토리지로 가는 PUT은 파트 번호를 기록해 재개 때 남은 파트만 보내는지 본다.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from sqlalchemy import text

from app.seed import DEV_A_CAMERA, DEV_A_SENSOR, SES_A
from tests.conftest import in_future
from tests.contract.openapi_contract import assert_matches_contract
from tests.integration.test_ingest import drain, mp4_bytes

pytestmark = pytest.mark.integration
MB = 1024 * 1024
EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "python" / "marine_client.py"
_ID_TEMPLATES = [(re.compile(r"upl_[0-9A-HJKMNP-TV-Z]{26}"), "{upload_id}"),
                 (re.compile(r"ast_[0-9A-HJKMNP-TV-Z]{26}"), "{asset_id}")]


def _example() -> ModuleType:
    spec = importlib.util.spec_from_file_location("marine_client", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["marine_client"] = module
    spec.loader.exec_module(module)
    return module


class ContractHook:
    """httpx 응답 훅: 예제가 받은 API 응답을 계약과 대조하고, 대조한 (작업, 상태)를 남긴다."""

    def __init__(self) -> None:
        self.checked: set[tuple[str, int]] = set()

    def __call__(self, response: httpx.Response) -> None:
        response.read()
        template = response.request.url.path
        for pattern, name in _ID_TEMPLATES:
            template = pattern.sub(name, template)
        assert_matches_contract(response, response.request.method, template)
        self.checked.add((f"{response.request.method} {template}", response.status_code))


class StorageRecorder(httpx.BaseTransport):
    """사전 서명 URL로 가는 요청을 실제 S3(moto)로 넘기며 파트 번호를 기록한다. fail_on 파트에서 연결을 끊는다."""

    def __init__(self, fail_on: set[int] | None = None) -> None:
        self.inner = httpx.HTTPTransport()
        self.fail_on = fail_on or set()
        self.sent: list[int] = []
        self.authorization: list[str | None] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        n = int(request.url.params["partNumber"])
        self.sent.append(n)
        self.authorization.append(request.headers.get("authorization"))
        if n in self.fail_on:
            raise httpx.ConnectError("합성 연결 끊김", request=request)
        return self.inner.handle_request(request)

    def close(self) -> None:
        self.inner.close()


@pytest.fixture()
def api_key(ictx) -> dict[str, str]:
    """기관 서버용 API 키 (ORG_ADMIN + MFA가 발급). 예제는 client_credentials로 단기 토큰을 받는다."""
    r = ictx.client.post("/v1/api-keys", headers=ictx.as_user(101, mfa=True),
                         json={"name": "example-client", "scopes": ["upload:write", "status:read", "asset:read"],
                               "expires_at": in_future(days=1)})
    assert r.status_code == 201, r.text
    return {"client_id": r.json()["client_id"], "client_secret": r.json()["client_secret"]}


def test_example_client_interrupted_upload_resumes_remaining_parts(ictx, api_key, tmp_path: Path) -> None:
    mc = _example()
    hook = ContractHook()
    ictx.client.event_hooks = {"request": [], "response": [hook]}
    data = mp4_bytes(11 * MB, seed=601)  # 합성 데이터. 시험 설정의 파트 크기 5MiB → 파트 3개
    src = tmp_path / "cam01_synthetic.mp4"
    src.write_bytes(data)
    state = tmp_path / "cam01_synthetic.mp4.marine-upload.json"
    manifest = mc.build_manifest(src, source_system_id=DEV_A_CAMERA, session_id=SES_A, data_type="VIDEO",
                                 variant="CLIP", mime_type="video/mp4", capture_start_at="2026-11-01T01:00:00Z",
                                 timestamp_source="DEVICE", timestamp_verified=True)

    # 1) 2번 파트를 보내다 연결이 끊긴다 → 예외, 상태 파일에 upload_id가 남는다
    cut = StorageRecorder(fail_on={2})
    with mc.MarineClient(http=ictx.client, storage=httpx.Client(transport=cut), **api_key) as client:
        with pytest.raises(httpx.ConnectError):
            client.upload_file(src, manifest, state_path=state)
        upload_id = json.loads(state.read_text(encoding="utf-8"))["upload_id"]
        assert [p["part_number"] for p in client.list_parts(upload_id)["completed_parts"]] == [1]
    assert cut.sent == [1, 2]
    assert cut.authorization == [None, None]  # 스토리지 요청에 API 토큰을 붙이지 않는다
    assert "secret" not in state.read_text(encoding="utf-8")

    # 2) 다시 실행(새 프로세스 흉내: 새 클라이언트) → 남은 파트 2·3만 보내고 완료한다
    rest = StorageRecorder()
    with mc.MarineClient(http=ictx.client, storage=httpx.Client(transport=rest), **api_key) as client:
        result = client.upload_file(src, manifest, state_path=state)
        assert (result.status, result.upload_id, result.ingest_status) == ("UPLOAD_COMPLETED", upload_id, "RECEIVED")
        assert result.parts_sent == [2, 3] and rest.sent == [2, 3]
        assert not state.exists()
        with pytest.raises(mc.ApiError) as err:  # 수신은 등록이 아니다: 검증 전에는 영수증이 없다
            client.get_receipt(result.asset_id)
        assert err.value.status == 404 and err.value.code == "NOT_FOUND"

        drain(ictx)  # 워커의 무결성 검증 작업
        settled = client.wait_until_settled(upload_id, timeout_s=5, interval_s=0)
        assert settled["ingest_status"] == "REGISTERED" and settled["parts_completed"] == 3
        receipt = client.get_receipt(result.asset_id)
        assert receipt["checksum_sha256"] == hashlib.sha256(data).hexdigest() and receipt["byte_size"] == len(data)
        assert client.get_asset(result.asset_id)["capture_start_at"] == "2026-11-01T01:00:00Z"

        # 3) 같은 파일을 다시 실행해도 아무것도 다시 보내지 않는다
        again = client.upload_file(src, manifest, state_path=state)  # 같은 Idempotency-Key → 처음 응답 재응답
        assert (again.status, again.upload_id, again.ingest_status) == ("ALREADY_COMPLETED", upload_id, "REGISTERED")
        other = client.upload_file(src, manifest, idempotency_key="example-other-key")  # 다른 키 → 서버 중복 판정
        assert (other.status, other.asset_id) == ("ALREADY_REGISTERED", result.asset_id)
    assert rest.sent == [2, 3]
    assert hook.checked >= {
        ("POST /v1/auth/token", 200), ("POST /v1/uploads", 201), ("POST /v1/uploads", 200),
        ("GET /v1/uploads/{upload_id}", 200), ("GET /v1/uploads/{upload_id}/parts", 200),
        ("POST /v1/uploads/{upload_id}/complete", 202), ("GET /v1/assets/{asset_id}/receipt", 404),
        ("GET /v1/assets/{asset_id}/receipt", 200), ("GET /v1/assets/{asset_id}", 200),
    }, sorted(hook.checked)


def test_example_client_user_token_problem_codes_and_expired_session(ictx, tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    mc = _example()
    hook = ContractHook()
    ictx.client.event_hooks = {"request": [], "response": [hook]}
    monkeypatch.setenv("MARINE_TOKEN", ictx.as_user(102)["Authorization"].removeprefix("Bearer "))
    monkeypatch.delenv("MARINE_CLIENT_ID", raising=False)
    monkeypatch.delenv("MARINE_CLIENT_SECRET", raising=False)
    csv = tmp_path / "buoy_synthetic.csv"
    csv.write_text("time,sea_water_temperature\n" + "".join(
        f"2026-11-01T00:{m:02d}:00Z,{18 + m / 100:.2f}\n" for m in range(60)), encoding="utf-8")
    storage = StorageRecorder()
    with mc.MarineClient.from_env(http=ictx.client, storage=httpx.Client(transport=storage)) as client:
        m = mc.build_manifest(csv, source_system_id=DEV_A_SENSOR, session_id=SES_A, data_type="SENSOR",
                              variant="ORIGINAL", mime_type="text/csv")
        assert m["timestamp_quality"] == {"source": "UNKNOWN", "verified": False} and "capture_start_at" not in m
        result = client.upload_file(csv, m)
        assert result.status == "UPLOAD_COMPLETED" and result.parts_sent == [1] and storage.sent == [1]
        drain(ictx)
        settled = client.wait_until_settled(result.upload_id, timeout_s=5, interval_s=0)
        assert settled["ingest_status"] in mc.REGISTERED_OR_LATER  # 센서 파일은 바로 READY_PRIVATE까지 간다
        assert client.get_receipt(result.asset_id)["byte_size"] == csv.stat().st_size

        # 오류는 problem+json의 code로 분기한다: C2 카메라에 원본(ORIGINAL)은 정책 위반
        video = tmp_path / "cam01_original.mp4"
        video.write_bytes(mp4_bytes(MB, seed=602))
        bad = mc.build_manifest(video, source_system_id=DEV_A_CAMERA, session_id=SES_A, data_type="VIDEO",
                                variant="ORIGINAL", mime_type="video/mp4")
        with pytest.raises(mc.ApiError) as err:
            client.upload_file(video, bad)
        assert (err.value.status, err.value.code) == (422, "RULE_VIOLATION")
        assert err.value.problem["allowed_variants"] == ["CLIP", "PROXY", "SAMPLE_FRAME"]
    assert storage.sent == [1]

    # 끊긴 세션이 유효 기간을 넘기면(410 GONE) 새 세션을 만들어 처음부터 다시 보낸다 (요구사항 3.3)
    video = tmp_path / "cam01_expiring.mp4"
    video.write_bytes(mp4_bytes(6 * MB, seed=603))  # 파트 2개
    m = mc.build_manifest(video, source_system_id=DEV_A_CAMERA, session_id=SES_A, data_type="VIDEO",
                          variant="CLIP", mime_type="video/mp4")
    state = tmp_path / "cam01_expiring.mp4.marine-upload.json"
    with mc.MarineClient.from_env(http=ictx.client, storage=httpx.Client(transport=StorageRecorder({2}))) as client:
        with pytest.raises(httpx.ConnectError):
            client.upload_file(video, m, state_path=state)
    expired = json.loads(state.read_text(encoding="utf-8"))["upload_id"]
    with ictx.c.engine.begin() as conn:
        conn.execute(text("UPDATE uploads SET expires_at = now() - interval '1 minute' WHERE upload_id = :u"),
                     {"u": expired})
    fresh = StorageRecorder()
    with mc.MarineClient.from_env(http=ictx.client, storage=httpx.Client(transport=fresh)) as client:
        result = client.upload_file(video, m, state_path=state)
    assert result.status == "UPLOAD_COMPLETED" and result.upload_id != expired and fresh.sent == [1, 2]
    assert not state.exists()
    assert {("POST /v1/uploads", 422), ("GET /v1/uploads/{upload_id}/parts", 410)} <= hook.checked
    assert ("POST /v1/auth/token", 200) not in hook.checked  # 사용자 토큰은 교환하지 않는다
