"""11번 단위 시험: 점검 기간·기한 계산, 감시 규칙 임계값 검사."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.domain import inspections as tpl
from app.services.monitoring import EVALUATORS, PARAM_TYPES, RULES, heartbeat_threshold_s, validate_params


@pytest.mark.parametrize(("cycle", "key", "last"), [
    ("DAILY", "2026-11-04", date(2026, 11, 4)),
    ("WEEKLY", "2026-W45", date(2026, 11, 8)),
    ("MONTHLY", "2026-02", date(2026, 2, 28)),
    ("MONTHLY", "2026-12", date(2026, 12, 31)),
    ("QUARTERLY", "2026-Q4", date(2026, 12, 31)),
    ("QUARTERLY", "2026-Q1", date(2026, 3, 31)),
    ("YEARLY", "2026", date(2026, 12, 31)),
])
def test_period_last_day(cycle: str, key: str, last: date) -> None:
    assert tpl.period_last_day(cycle, key) == last


def test_period_format_must_match_cycle() -> None:
    for cycle, key in (("DAILY", "2026-11"), ("WEEKLY", "2026-11-04"), ("QUARTERLY", "2026-Q5"), ("YEARLY", "26")):
        with pytest.raises(ValueError):
            tpl.period_last_day(cycle, key)


def test_due_at_is_end_of_period_in_ops_timezone() -> None:
    assert tpl.due_at("DAILY", "2026-11-03", "Asia/Seoul").astimezone(UTC) == datetime(2026, 11, 3, 15, tzinfo=UTC)


def test_previous_period() -> None:
    d = date(2026, 11, 4)  # 수요일
    assert [tpl.previous_period(c, d) for c in tpl.CYCLES] == ["2026-11-03", "2026-W44", "2026-10", "2026-Q3", "2025"]
    assert tpl.previous_period("QUARTERLY", date(2026, 1, 15)) == "2025-Q4"


def test_templates_follow_baseline_15_2() -> None:
    assert {t.cycle for t in tpl.TEMPLATES} == set(tpl.CYCLES)
    assert len({t.item_code for t in tpl.TEMPLATES}) == len(tpl.TEMPLATES)


def test_validate_params() -> None:
    assert validate_params("HEARTBEAT_MISSING", {"factor": 2.5, "min_silence_s": 120}) == []
    assert validate_params("HEARTBEAT_MISSING", {"min_silence_s": 1.5}) == ["min_silence_s: 정수여야 합니다"]
    assert validate_params("HEARTBEAT_MISSING", {"factor": True}) == ["factor: 숫자여야 합니다"]
    assert validate_params("HEARTBEAT_MISSING", {"factor": 0}) == ["factor: 0보다 커야 합니다"]
    assert validate_params("HARDWARE_SIGNAL", {"min_status": "OK"}) == ["min_status: WARNING, CRITICAL 중 하나"]
    assert validate_params("INGEST_LAG", {"x": 1}) == ["알 수 없는 임계값: x"]


def test_every_rule_has_text_params_and_evaluator_or_pending_source() -> None:
    assert set(RULES) == set(PARAM_TYPES)
    assert set(RULES) == set(EVALUATORS)  # OPENAPI_FAILURE도 12번에서 신호원 연결


def test_heartbeat_threshold() -> None:
    assert heartbeat_threshold_s({"factor": 3, "min_silence_s": 300}, 60) == 300
    assert heartbeat_threshold_s({"factor": 3, "min_silence_s": 300}, 600) == 1800
