"""Alerting — Telegram, email."""

from __future__ import annotations

import smtplib
from email.mime.text import MIMEText

import httpx

from shared.config import get_settings
from shared.logging import audit


class AlertNotifier:
    async def send(self, title: str, message: str, level: str = "info") -> bool:
        cfg = get_settings()
        text = f"[{level.upper()}] {title}\n{message}"
        sent = False
        if cfg.telegram_bot_token and cfg.telegram_chat_id:
            sent = await self._telegram(text) or sent
        if cfg.alert_email_to and cfg.smtp_host:
            sent = self._email(title, text) or sent
        audit("alert_sent", title=title, level=level, delivered=sent)
        return sent

    async def _telegram(self, text: str) -> bool:
        cfg = get_settings()
        url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(url, json={"chat_id": cfg.telegram_chat_id, "text": text[:4000]})
                return r.status_code == 200
        except Exception:
            return False

    def _email(self, subject: str, body: str) -> bool:
        cfg = get_settings()
        try:
            msg = MIMEText(body)
            msg["Subject"] = f"Apex Trader: {subject}"
            msg["From"] = cfg.smtp_user
            msg["To"] = cfg.alert_email_to
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port) as s:
                s.starttls()
                s.login(cfg.smtp_user, cfg.smtp_password)
                s.send_message(msg)
            return True
        except Exception:
            return False
