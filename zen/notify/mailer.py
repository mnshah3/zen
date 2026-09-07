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


def send(subject: str, html: str, text: str = "",
         images: dict[str, bytes] | None = None) -> bool:
    """Send one HTML email, optionally with inline charts.

    Images are attached to the message and referenced from the HTML as
    cid:<name>. Gmail renders those immediately, whereas a remote <img src>
    is blocked until the reader clicks "display images" -- which, for a brief
    read on a phone at 7am, means the charts are never seen.
    """
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

    if images:
        html_part = msg.get_payload()[-1]
        for name, blob in images.items():
            html_part.add_related(blob, maintype="image", subtype="png",
                                  cid=f"<{name}>", filename=f"{name}.png")

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
