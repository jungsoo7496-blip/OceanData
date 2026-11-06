"""06번·ADR-0040: 장비 정책 숫자 항목의 기술 상한 (DB 없이).

에이전트·서버가 시간·크기·동시 실행 수로 바꾸는 숫자는 계약(device-policy.schema.json)에 상한이 있다. 상한은 제품 한도가
아니라 아래 계산이 넘치지 않는 기술 한도이고, 상한까지는 통과하고 넘으면 set-policy·정책 초안이 쓰는 계약 검사가 거부한다.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import yaml

from app.services.contracts import contracts_dir, schema_files, schema_problems
from app.services.monitoring import policy_heartbeat_interval_s

POLICY = json.loads((contracts_dir() / "examples/device-policy.vessel-camera.json").read_text(encoding="utf-8"))
INT32_MAX = 2**31 - 1
INT64_MAX = 2**63 - 1
# 퍼즈로 찾은 넘침 값: Go의 time.Duration(n) * time.Second가 int64 나노초를 넘어 음수가 되는 가장 작은 초
GO_DURATION_OVERFLOW_S = INT64_MAX // 10**9 + 1
BOUNDS = [  # (JSON 포인터, 계약 상한)
    ("/heartbeat_interval_s", INT32_MAX),
    ("/size_stable_s", INT32_MAX),
    ("/max_parallel_parts", 10000),
    ("/sources/0/expected_interval_s", INT32_MAX),
    ("/sources/0/local_retention_days", 24855),
    ("/transfer_windows/0/max_kbps", INT32_MAX),
    ("/watch_rules/0/priority", INT32_MAX),
]


def _with(pointer: str, value: int) -> dict[str, Any]:
    doc = copy.deepcopy(POLICY)
    *parents, last = [int(p) if p.isdigit() else p for p in pointer.strip("/").split("/")]
    node: Any = doc
    for key in parents:
        node = node[key]
    node[last] = value
    return doc


@pytest.mark.parametrize(("pointer", "limit"), BOUNDS)
def test_policy_numbers_have_technical_maximum(pointer: str, limit: int) -> None:
    assert schema_problems("device-policy", _with(pointer, limit)) == []
    for value in (limit + 1, GO_DURATION_OVERFLOW_S):
        found = schema_problems("device-policy", _with(pointer, value))
        assert [(p.pointer, p.code) for p in found] == [(pointer, "schema.maximum")], value


def test_bounds_do_not_overflow_downstream_arithmetic() -> None:
    assert GO_DURATION_OVERFLOW_S == 9223372037
    # 초 단위 상한: 에이전트가 time.Duration(int64 나노초)으로 바꿔도 넘치지 않는다
    assert INT32_MAX * 10**9 <= INT64_MAX
    # 일 단위 상한(현장 보존 기간): 초로 바꿔도 같은 32비트 초 상한 안이고, 하루만 더해도 넘는다
    assert 24855 * 86400 <= INT32_MAX < 24856 * 86400
    # 서버 수집 정상성 판정은 마지막 수신 시각에 주기를 더한다 (Python datetime은 9999년까지)
    assert (datetime(2026, 11, 5, tzinfo=UTC) + timedelta(seconds=INT32_MAX)).year == 2094
    api = yaml.safe_load((contracts_dir() / "openapi.yaml").read_text(encoding="utf-8"))
    # 파트 동시 전송 수 상한 = 업로드 파트 번호 상한: 동시에 보낼 파트가 그보다 많을 수 없다
    assert api["components"]["schemas"]["PartUrl"]["properties"]["part_number"]["maximum"] == 10000
    # 장비 등록 값은 기본 정책의 sources 항목이 되므로 범위가 같아야 한다 (정책 응답이 계약을 어기지 않게)
    device = api["components"]["schemas"]["DeviceCreate"]["properties"]
    source = schema_files()["device-policy"]["properties"]["sources"]["items"]["properties"]
    for name in ("local_retention_days", "expected_interval_s"):
        assert (device[name]["minimum"], device[name]["maximum"]) == (source[name]["minimum"], source[name]["maximum"])


def test_monitoring_uses_default_for_heartbeat_interval_stored_before_the_bound() -> None:
    assert policy_heartbeat_interval_s({}) == 60
    assert policy_heartbeat_interval_s({"heartbeat_interval_s": 300}) == 300
    assert policy_heartbeat_interval_s({"heartbeat_interval_s": INT32_MAX}) == INT32_MAX
    assert policy_heartbeat_interval_s({"heartbeat_interval_s": 10**20}) == 60
