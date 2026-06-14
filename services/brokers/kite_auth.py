"""Kite OAuth session — daily login, token persistence, refresh."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial

from shared.config import get_settings
from shared.database import SessionLocal
from shared.logging import audit
from shared.models import KiteSession


@dataclass
class KiteSessionInfo:
    access_token: str = ""
    refresh_token: str = ""
    user_id: str = ""
    user_name: str = ""
    login_time: datetime | None = None


class KiteAuthService:
    _SESSION_ID = 1

    def __init__(self) -> None:
        self.cfg = get_settings()
        self._session: KiteSessionInfo | None = None

    def get_access_token_sync(self) -> str:
        if self._session and self._session.access_token:
            return self._session.access_token
        return self.cfg.kite_access_token or ""

    async def startup(self) -> None:
        await self._load_from_db()
        await self.try_renew()

    def login_url(self) -> str:
        if not self.cfg.kite_api_key:
            raise ValueError("KITE_API_KEY is not configured")
        from kiteconnect import KiteConnect
        return KiteConnect(api_key=self.cfg.kite_api_key).login_url()

    async def complete_login(self, request_token: str) -> KiteSessionInfo:
        if not self.cfg.kite_api_key or not self.cfg.kite_api_secret:
            raise ValueError("KITE_API_KEY and KITE_API_SECRET are required")
        from kiteconnect import KiteConnect

        kite = KiteConnect(api_key=self.cfg.kite_api_key)
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None,
            partial(kite.generate_session, request_token, self.cfg.kite_api_secret),
        )
        self._session = KiteSessionInfo(
            access_token=str(data.get("access_token", "")),
            refresh_token=str(data.get("refresh_token", "") or ""),
            user_id=str(data.get("user_id", "") or ""),
            user_name=str(data.get("user_name", "") or ""),
            login_time=datetime.now(timezone.utc),
        )
        await self._save_to_db()
        audit("kite_login_success", user_id=self._session.user_id)
        return self._session

    async def disconnect(self) -> None:
        token = self.get_access_token_sync()
        if token and self.cfg.kite_api_key:
            try:
                from kiteconnect import KiteConnect

                kite = KiteConnect(api_key=self.cfg.kite_api_key)
                kite.set_access_token(token)
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, partial(kite.invalidate_access_token, token))
            except Exception as e:
                audit("kite_invalidate_failed", error=str(e))
        self._session = None
        await self._clear_db()
        audit("kite_disconnected")

    async def try_renew(self) -> bool:
        if not self._session or not self._session.refresh_token:
            return False
        if not self.cfg.kite_api_key or not self.cfg.kite_api_secret:
            return False
        try:
            from kiteconnect import KiteConnect

            kite = KiteConnect(api_key=self.cfg.kite_api_key)
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(
                None,
                partial(
                    kite.renew_access_token,
                    self._session.refresh_token,
                    self.cfg.kite_api_secret,
                ),
            )
            self._session.access_token = str(data.get("access_token", ""))
            refresh = data.get("refresh_token")
            if refresh:
                self._session.refresh_token = str(refresh)
            await self._save_to_db()
            audit("kite_token_renewed", user_id=self._session.user_id)
            return True
        except Exception as e:
            audit("kite_renew_failed", error=str(e))
            return False

    async def verify_connection(self) -> bool:
        token = self.get_access_token_sync()
        if not token or not self.cfg.kite_api_key:
            return False
        try:
            from kiteconnect import KiteConnect

            kite = KiteConnect(api_key=self.cfg.kite_api_key)
            kite.set_access_token(token)
            loop = asyncio.get_event_loop()
            profile = await loop.run_in_executor(None, kite.profile)
            if self._session:
                self._session.user_id = str(
                    profile.get("user_id", "") or self._session.user_id
                )
                self._session.user_name = str(
                    profile.get("user_name", "") or self._session.user_name
                )
            return True
        except Exception:
            return False

    async def get_status(self) -> dict:
        has_credentials = bool(self.cfg.kite_api_key and self.cfg.kite_api_secret)
        token = self.get_access_token_sync()
        source = "none"
        if self._session and self._session.access_token:
            source = "session"
        elif self.cfg.kite_access_token:
            source = "env"

        connected = await self.verify_connection() if token and has_credentials else False
        login_time = None
        user_name = ""
        user_id = ""
        if self._session:
            login_time = (
                self._session.login_time.isoformat() if self._session.login_time else None
            )
            user_name = self._session.user_name
            user_id = self._session.user_id

        return {
            "configured": has_credentials,
            "connected": connected,
            "user_id": user_id,
            "user_name": user_name,
            "token_source": source,
            "login_time": login_time,
            "redirect_url": self.cfg.kite_redirect_url,
            "login_url": self.login_url() if has_credentials else None,
            "needs_daily_login": True,
            "message": (
                "Connected to Zerodha"
                if connected
                else (
                    "Click Connect Zerodha to log in (required each trading day)"
                    if has_credentials
                    else "Set KITE_API_KEY and KITE_API_SECRET in .env"
                )
            ),
        }

    async def _load_from_db(self) -> None:
        try:
            async with SessionLocal() as session:
                row = await session.get(KiteSession, self._SESSION_ID)
                if row is None or not row.access_token:
                    return
                self._session = KiteSessionInfo(
                    access_token=row.access_token or "",
                    refresh_token=row.refresh_token or "",
                    user_id=row.user_id or "",
                    user_name=row.user_name or "",
                    login_time=row.login_time,
                )
        except Exception as e:
            audit("kite_session_load_failed", error=str(e))

    async def _save_to_db(self) -> None:
        if not self._session:
            return
        try:
            async with SessionLocal() as session:
                row = await session.get(KiteSession, self._SESSION_ID)
                if row is None:
                    row = KiteSession(id=self._SESSION_ID)
                    session.add(row)
                row.access_token = self._session.access_token
                row.refresh_token = self._session.refresh_token or None
                row.user_id = self._session.user_id or None
                row.user_name = self._session.user_name or None
                row.login_time = self._session.login_time
                await session.commit()
        except Exception as e:
            audit("kite_session_save_failed", error=str(e))

    async def _clear_db(self) -> None:
        try:
            async with SessionLocal() as session:
                row = await session.get(KiteSession, self._SESSION_ID)
                if row:
                    row.access_token = None
                    row.refresh_token = None
                    row.user_id = None
                    row.user_name = None
                    row.login_time = None
                    await session.commit()
        except Exception as e:
            audit("kite_session_clear_failed", error=str(e))


kite_auth = KiteAuthService()
