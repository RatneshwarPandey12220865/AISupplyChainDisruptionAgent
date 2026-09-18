"""Alerting: always recorded in the DB (shown in the dashboard), plus Slack webhook and SMTP email if configured."""
import logging
import smtplib
from email.message import EmailMessage

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Notification

log = logging.getLogger(__name__)


def notify(session: Session, title: str, body: str, level: str = "info",
           disruption_id: int | None = None) -> Notification:
    channels = ["dashboard"]
    if settings.slack_webhook_url:
        try:
            emoji = {"critical": ":rotating_light:", "warning": ":warning:"}.get(level, ":information_source:")
            httpx.post(settings.slack_webhook_url, json={"text": f"{emoji} *{title}*\n{body}"},
                       timeout=10).raise_for_status()
            channels.append("slack")
        except Exception as exc:
            log.warning("Slack notification failed: %s", exc)
    if settings.smtp_host and settings.alert_email_to:
        try:
            msg = EmailMessage()
            msg["Subject"] = f"[Supply Chain Agent] {title}"
            msg["From"] = settings.smtp_user or "agent@localhost"
            msg["To"] = settings.alert_email_to
            msg.set_content(body)
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
                smtp.starttls()
                if settings.smtp_user:
                    smtp.login(settings.smtp_user, settings.smtp_password)
                smtp.send_message(msg)
            channels.append("email")
        except Exception as exc:
            log.warning("Email notification failed: %s", exc)
    note = Notification(level=level, title=title, body=body, channels=channels, disruption_id=disruption_id)
    session.add(note)
    session.flush()
    log.info("[%s] %s", level.upper(), title)
    return note
