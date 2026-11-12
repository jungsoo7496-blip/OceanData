"""04번 요구사항 10.2·ADR-0053: 예제 클라이언트는 Retry-After가 붙은 409(같은 키의 앞선 요청이 처리 중이거나 동시 요청과 겹침)도
기다렸다 다시 보낸다. Retry-After 없는 409(IDEMPOTENCY_CONFLICT·업무 충돌)는 다시 보내도 같으므로 바로 오류로 올린다. 값은 합성이다."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest

EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "python" / "marine_client.py"
MANIFEST = {"schema_version": "1.0.0", "source_system_id": "dev_00000000000000000000000302",
            "file": {"checksum_sha256": "a" * 64}}


def _example() -> ModuleType:
    spec = importlib.util.spec_from_file_location("marine_client", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["marine_client"] = module
    spec.loader.exec_module(module)
    return module


def _client(mc: ModuleType, replies: list[httpx.Response], waits: list[float]):  # type: ignore[no-untyped-def]
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return replies[min(len(calls), len(replies)) - 1]

    http = httpx.Client(base_url="http://api.test", transport=httpx.MockTransport(handler))
    return mc.MarineClient(token="t" * 20, http=http, sleep=waits.append), calls


def test_conflict_with_retry_after_is_retried_with_the_same_key() -> None:
    mc, waits = _example(), []
    busy = httpx.Response(409, headers={"Retry-After": "2"}, json={"code": "CONFLICT", "status": 409})
    ok = httpx.Response(201, headers={"Idempotent-Replayed": "true"}, json={"status": "UPLOAD_CREATED"})
    client, calls = _client(mc, [busy, ok], waits)

    body, replayed = client.create_upload(MANIFEST, idempotency_key="agent-key-0001")

    assert body == {"status": "UPLOAD_CREATED"} and replayed is True
    assert waits == [2.0] and len(calls) == 2
    assert {r.headers["idempotency-key"] for r in calls} == {"agent-key-0001"}  # 재시도는 같은 키


def test_conflict_without_retry_after_is_not_retried() -> None:
    mc, waits = _example(), []
    conflict = httpx.Response(409, json={"code": "IDEMPOTENCY_CONFLICT", "status": 409})
    client, calls = _client(mc, [conflict], waits)

    with pytest.raises(mc.ApiError) as err:
        client.create_upload(MANIFEST, idempotency_key="agent-key-0002")

    assert err.value.code == "IDEMPOTENCY_CONFLICT" and waits == [] and len(calls) == 1
