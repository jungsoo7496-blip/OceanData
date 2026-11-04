"""InferencePort — GPU 의존 코드는 이 뒤에만 둔다 (tech.md HW-2). 구현은 05·14번 스펙."""

from __future__ import annotations

from typing import Any, Protocol


class InferencePort(Protocol):
    def detect(self, model_id: str, frame: bytes) -> list[dict[str, Any]]: ...


class NotImplementedInference:
    def detect(self, model_id: str, frame: bytes) -> list[dict[str, Any]]:
        raise NotImplementedError("InferencePort는 05·14번 스펙에서 구현한다")
