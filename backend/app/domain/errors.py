"""도메인 예외. HTTP 변환은 app/errors.py가 맡는다."""

from __future__ import annotations

from dataclasses import dataclass


class DomainError(Exception):
    code = "CONFLICT"


@dataclass
class UnmetCondition:
    condition: str
    actual: str | None = None


class RuleViolation(DomainError):
    code = "RULE_VIOLATION"

    def __init__(self, detail: str, unmet: list[UnmetCondition]) -> None:
        super().__init__(detail)
        self.detail = detail
        self.unmet = unmet


class InvalidStateTransition(DomainError):
    code = "INVALID_STATE_TRANSITION"

    def __init__(self, field: str, current: str | None, target: str, actor_role: str | None = None) -> None:
        super().__init__(field)
        self.field, self.current, self.target, self.actor_role = field, current, target, actor_role

    def __str__(self) -> str:
        return f"{self.field}: {self.current} → {self.target} 전이는 허용되지 않습니다"
