"""정기 점검 항목 템플릿과 점검 기간 계산 (11번 요구사항 6). 항목은 기준 문서 15.2장.

auto=True 항목은 서버가 결과를 채운다 (요구사항 6.4). 신호원이 아직 없으면 AUTO_UNAVAILABLE로 채우고
담당자가 직접 확인한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class ItemTemplate:
    cycle: str
    item_code: str
    title: str
    auto: bool = False


TEMPLATES: tuple[ItemTemplate, ...] = (
    ItemTemplate("DAILY", "D_INGEST", "수집 성공/실패", auto=True),
    ItemTemplate("DAILY", "D_QUARANTINE", "격리 건", auto=True),
    ItemTemplate("DAILY", "D_STORAGE", "저장 사용률", auto=True),
    ItemTemplate("DAILY", "D_BACKUP", "백업 성공", auto=True),
    ItemTemplate("DAILY", "D_HARDWARE", "하드웨어 경고", auto=True),
    ItemTemplate("WEEKLY", "W_SMART", "디스크 SMART", auto=True),
    ItemTemplate("WEEKLY", "W_GPU_BACKLOG", "GPU 적체", auto=True),
    ItemTemplate("WEEKLY", "W_UNREVIEWED", "미검수 건"),
    ItemTemplate("WEEKLY", "W_SECURITY", "보안 이벤트"),
    ItemTemplate("WEEKLY", "W_SUPPORT", "기업 지원 티켓"),
    ItemTemplate("MONTHLY", "M_HASH_SAMPLE", "해시 표본 검증", auto=True),
    ItemTemplate("MONTHLY", "M_RETENTION", "보존 정책 실행", auto=True),
    ItemTemplate("MONTHLY", "M_CAPACITY", "용량 예측", auto=True),
    ItemTemplate("MONTHLY", "M_ACCOUNTS", "계정·권한 정리"),
    ItemTemplate("MONTHLY", "M_PATCH", "패치"),
    ItemTemplate("MONTHLY", "M_STANDARD", "표준 변경 관리"),
    ItemTemplate("QUARTERLY", "Q_RESTORE", "백업 복원 시험"),
    ItemTemplate("QUARTERLY", "Q_CROSS_ACCESS", "교차 접근 시험"),
    ItemTemplate("QUARTERLY", "Q_DEID", "비식별 품질 재검수"),
    ItemTemplate("QUARTERLY", "Q_DRILL", "장애·침해 훈련"),
    ItemTemplate("YEARLY", "Y_SECURITY", "외부 보안 점검"),
    ItemTemplate("YEARLY", "Y_HW_LIFECYCLE", "하드웨어 노후·지원 기간"),
    ItemTemplate("YEARLY", "Y_TCO", "3년 TCO 재평가"),
    ItemTemplate("YEARLY", "Y_AGREEMENT", "약정 개정"),
)
CYCLES = ("DAILY", "WEEKLY", "MONTHLY", "QUARTERLY", "YEARLY")
_PATTERNS = {
    "DAILY": re.compile(r"^(\d{4})-(\d{2})-(\d{2})$"),
    "WEEKLY": re.compile(r"^(\d{4})-W(\d{2})$"),
    "MONTHLY": re.compile(r"^(\d{4})-(\d{2})$"),
    "QUARTERLY": re.compile(r"^(\d{4})-Q([1-4])$"),
    "YEARLY": re.compile(r"^(\d{4})$"),
}


def items_for(cycle: str) -> list[ItemTemplate]:
    return [t for t in TEMPLATES if t.cycle == cycle]


def period_last_day(cycle: str, period_key: str) -> date:
    """기간의 마지막 날. 형식이 주기와 맞지 않으면 ValueError."""
    m = _PATTERNS[cycle].match(period_key)
    if not m:
        raise ValueError(f"{cycle} 기간 형식이 아닙니다: {period_key}")
    y = int(m.group(1))
    if cycle == "DAILY":
        return date(y, int(m.group(2)), int(m.group(3)))
    if cycle == "WEEKLY":
        return date.fromisocalendar(y, int(m.group(2)), 7)
    if cycle == "MONTHLY":
        mo = int(m.group(2))
        first_next = date(y + (mo == 12), mo % 12 + 1, 1)
        return first_next - timedelta(days=1)
    if cycle == "QUARTERLY":
        q = int(m.group(2))
        return date(y + (q == 4), (q * 3) % 12 + 1, 1) - timedelta(days=1)
    return date(y, 12, 31)


def due_at(cycle: str, period_key: str, tz: str) -> datetime:
    """기한 = 기간 마지막 날 24시 (운영 시간대)."""
    last = period_last_day(cycle, period_key)
    return datetime.combine(last + timedelta(days=1), time.min, tzinfo=ZoneInfo(tz))


def period_key_for(cycle: str, d: date) -> str:
    if cycle == "DAILY":
        return d.isoformat()
    if cycle == "WEEKLY":
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"
    if cycle == "MONTHLY":
        return f"{d.year}-{d.month:02d}"
    if cycle == "QUARTERLY":
        return f"{d.year}-Q{(d.month - 1) // 3 + 1}"
    return str(d.year)


def previous_period(cycle: str, today: date) -> str:
    """오늘이 속한 기간의 바로 앞 기간 (기한이 이미 지난 가장 최근 기간)."""
    current_first = {
        "DAILY": today,
        "WEEKLY": today - timedelta(days=today.weekday()),
        "MONTHLY": today.replace(day=1),
        "QUARTERLY": today.replace(month=(today.month - 1) // 3 * 3 + 1, day=1),
        "YEARLY": today.replace(month=1, day=1),
    }[cycle]
    return period_key_for(cycle, current_first - timedelta(days=1))
