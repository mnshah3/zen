"""Email delivery over SMTP.

Credentials come from the environment only. For Gmail use an App Password
(Google Account -> Security -> 2-Step Verification -> App passwords), never
the account password.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, formatdate

log = logging.getLogger(__name__)


def _config() -> dict | None:
    cfg = {
        "host": os.environ.get("SMTP_HOST", "smtp.gmail.com"),
        "port": int(os.environ.get("SMTP_PORT", "587")),
        "user": os.environ.get("SMTP_USER"),
        "password": os.environ.get("SMTP_PASSWORD"),
        "to": os.environ.get("MAIL_TO"),
    }
    if not all([cfg["user"], cfg["password"], cfg["to"]]):
        return None
    return cfg


def send(subject: str, html: str, text: str = "") -> bool:
    """Send one HTML email. Returns False (and dumps to stdout) if unconfigured."""
    cfg = _config()
    if cfg is None:
        log.warning("SMTP not configured; writing to stdout instead")
        print(f"--- SUBJECT: {subject} ---")
        print(text or html[:3000])
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("Zen", cfg["user"]))
    msg["To"] = cfg["to"]
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(text or "This brief is best viewed as HTML.")
    msg.add_alternative(html, subtype="html")

    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as s:
            s.starttls()
            s.login(cfg["user"], cfg["password"])
            s.send_message(msg)
    except Exception as e:
        log.error("email send failed: %s", e)
        return False

    log.info("sent %r to %s", subject, cfg["to"])
    return True
