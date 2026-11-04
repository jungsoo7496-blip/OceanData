"""포트 인터페이스 (structure.md 2장). 하드웨어가 바뀌면 adapters/만 교체한다."""

from app.ports.clock import ClockPort
from app.ports.external_source import ExternalSourcePort
from app.ports.identity import IdentityPort, TokenClaims
from app.ports.inference import InferencePort
from app.ports.notifier import Notification, NotifierPort
from app.ports.queue import Job, QueuePort
from app.ports.report_renderer import ReportRendererPort
from app.ports.storage import StoragePort

__all__ = [
    "ClockPort", "ExternalSourcePort", "IdentityPort", "InferencePort", "Job", "Notification",
    "NotifierPort", "QueuePort", "ReportRendererPort", "StoragePort", "TokenClaims",
]
