"""Telegram delivery. Token and chat id come from env, never from code."""

from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/sendMessage"


def send(text: str, *, silent: bool = False) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; printing instead")
        print(text)
        return False

    r = requests.post(
        API.format(token=token),
        json={"chat_id": chat_id, "text": text[:4096],
              "parse_mode": "HTML", "disable_notification": silent},
        timeout=20,
    )
    if r.status_code != 200:
        log.error("telegram failed %s: %s", r.status_code, r.text[:300])
        return False
    return True
