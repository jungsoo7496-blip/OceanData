"""계약 전체(모든 작업) Schemathesis 탐색 — 기본은 건너뛴다. `SCHEMATHESIS_ALL=1`일 때만 돈다 (04번 요구사항 10.2 보강).

CI의 test_schemathesis.py는 수집 흐름 20개 작업만 결정적으로 본다. 이 파일은 나머지 스펙의 작업까지 역할별 토큰
(제공기관 구성원·운영자 다중 인증)으로 양성·음성 입력을 보내 5xx·문서에 없는 상태 코드·응답 스키마 위반을 찾는다.
작업마다 예제 수를 줄여 한 번에 훑고, 실패는 작업별로 모아 보고한다. 찾은 것은 계약·앱을 고친 뒤 회귀 시험으로 남긴다.
  SCHEMATHESIS_ALL=1 SCHEMATHESIS_ALL_EXAMPLES=5 pytest -q tests/contract/test_schemathesis_all.py
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from hypothesis import HealthCheck, Phase, given, settings
from hypothesis import strategies as st
from hypothesis.errors import Unsatisfiable
from schemathesis import GenerationMode

from tests.contract.test_schemathesis import (
    NEGATIVE_CHECKS,
    POSITIVE_CHECKS,
    ContractEnv,
    _checks,
    _generatable,
    contract_env,  # noqa: F401 - pytest 픽스처
)

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(os.environ.get("SCHEMATHESIS_ALL") != "1", reason="SCHEMATHESIS_ALL=1일 때만")]
EXAMPLES = int(os.environ.get("SCHEMATHESIS_ALL_EXAMPLES", "5"))
METHODS = ("get", "post", "put", "patch", "delete")
# 실행하면 다른 시험의 전제를 바꾸거나 오래 걸리는 작업, Schemathesis가 본문 스키마(allOf+anyOf 조합)를 만들지 못하는
# 작업(POST /v1/events — 07번 계약 시험이 따로 본다)은 뺀다
SKIP = {"POST /v1/backup-runs", "POST /v1/auth/token", "POST /v1/agents/enroll", "POST /v1/events"}
# 교차 항목·참조·어휘 같은 업무 규칙은 무작위지만 스키마에 맞는 값을 정당하게 400·422로 거절한다 → 탐색에서는
# positive_data_acceptance를 빼고, 서버가 스키마 수준에서 더 엄격한지는 test_request_constraints.py가 결정적으로 본다
EXPLORE_POSITIVE = tuple(c for c in POSITIVE_CHECKS if c != "positive_data_acceptance")


def _labels(env: ContractEnv) -> list[str]:
    raw = env.schema.raw_schema["paths"]
    return [f"{m.upper()} {p}" for p, item in raw.items() for m in METHODS if m in item
            and f"{m.upper()} {p}" not in SKIP]


def _summary(exc: BaseException) -> str:
    text = str(exc)
    subs = getattr(exc, "exceptions", None)
    if subs:
        text += "\n" + "\n".join(f"  - {type(e).__name__}: {str(e)[:600]}" for e in subs)
    return text[:2500]


def _explore(env: ContractEnv, headers: dict[str, str], mode: GenerationMode, names: tuple[str, ...]) -> list[str]:
    checks = _checks(names)
    failures: list[str] = []
    for label in _labels(env):
        try:
            strategy = env.operation(label).as_strategy(generation_mode=mode).filter(_generatable)
        except Exception as exc:  # noqa: BLE001 - 전략을 못 만드는 작업도 보고
            failures.append(f"### {label} (전략 생성 실패)\n{exc}")
            continue

        @settings(max_examples=EXAMPLES, derandomize=True, deadline=None, database=None,
                  suppress_health_check=list(HealthCheck), phases=[Phase.explicit, Phase.generate])
        @given(case=strategy, pick=st.integers(min_value=0, max_value=3))
        def run(case: Any, pick: int) -> None:
            if pick and case.path_parameters:
                for name, values in env.seeded.items():  # 문자열 경로 값만 시드 ID로 (정수 version 등은 그대로)
                    if isinstance(case.path_parameters.get(name), str):
                        case.path_parameters[name] = values[pick % len(values)]
            if "Authorization" in (case.headers or {}):  # 인증을 뺀 음성 입력(401 확인)은 그대로 둔다
                case.headers = {**case.headers, **headers}
            case.call_and_validate(checks=checks)

        try:
            run()
        except Unsatisfiable:  # 매개변수·본문이 없어 음성 입력을 만들 수 없는 작업 (/healthz 등)
            continue
        except (Exception, BaseExceptionGroup) as exc:  # noqa: BLE001 - FailureGroup은 BaseExceptionGroup이다
            failures.append(f"### {label}\n{_summary(exc)}")
    return failures


@pytest.mark.parametrize("who", ["member", "operator"])
def test_schemathesis_all_operations(contract_env: ContractEnv, who: str) -> None:  # noqa: F811 - 가져온 픽스처
    env = contract_env
    if who == "operator":
        from app.seed import sub
        from tests.conftest import user_token

        headers = {"Authorization": f"Bearer {user_token(sub(106), mfa=True)}"}
    else:
        headers = env.headers
    failures = _explore(env, headers, GenerationMode.POSITIVE, EXPLORE_POSITIVE)
    failures += _explore(env, headers, GenerationMode.NEGATIVE, NEGATIVE_CHECKS)
    report = os.environ.get("SCHEMATHESIS_ALL_REPORT")
    if report:
        with open(f"{report}.{who}.txt", "w", encoding="utf-8") as f:
            f.write("\n\n".join(failures))
    assert not failures, f"{len(failures)}개 작업이 계약과 다르다:\n\n" + "\n\n".join(failures)
