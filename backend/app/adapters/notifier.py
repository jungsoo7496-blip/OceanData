"""NotifierPort → 로그 + (설정 시) 메일. 운영 메신저는 확정 후 어댑터 추가 (tech.md 결정 대기)."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from app.ports.notifier import Notification

log = logging.getLogger("notify")


class LogNotifier:
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def send(self, notification: Notification) -> None:
        self.sent.append(notification)
        log.info("notification", extra={"kind": notification.kind,
                                        "target_organization_id": notification.organization_id,
                                        "recipient_count": len(notification.recipients)})


class SmtpNotifier(LogNotifier):
    def __init__(self, host: str, port: int, sender: str) -> None:
        super().__init__()
        self.host, self.port, self.sender = host, port, sender

    def send(self, notification: Notification) -> None:
        super().send(notification)
        if not notification.recipients:
            return
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, ", ".join(notification.recipients), notification.subject
        msg.set_content(notification.body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=5) as s:
                s.send_message(msg)
        except OSError as exc:
            log.warning("notification_mail_failed", extra={"error_type": type(exc).__name__})
