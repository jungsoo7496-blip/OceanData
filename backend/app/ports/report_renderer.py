"""ReportRendererPort — PDF·XLSX·HWPX (10번 스펙)."""

from __future__ import annotations

from typing import Any, Protocol


class ReportRendererPort(Protocol):
    format: str

    def render(self, template_id: str, context: dict[str, Any]) -> bytes: ...


class NotImplementedReportRenderer:
    format = "NONE"

    def render(self, template_id: str, context: dict[str, Any]) -> bytes:
        raise NotImplementedError("ReportRendererPort는 10번 스펙에서 구현한다")
