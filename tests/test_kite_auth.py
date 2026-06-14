"""Kite OAuth session tests."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from services.brokers.kite_auth import KiteAuthService, KiteSessionInfo


@pytest.fixture
def auth(monkeypatch):
    monkeypatch.setenv("KITE_API_KEY", "test_key")
    monkeypatch.setenv("KITE_API_SECRET", "test_secret")
    monkeypatch.setenv("KITE_ACCESS_TOKEN", "")
    from shared.config import get_settings
    get_settings.cache_clear()
    service = KiteAuthService()
    service._session = None
    return service


def test_get_access_token_prefers_session(auth):
    auth._session = KiteSessionInfo(access_token="session_token")
    assert auth.get_access_token_sync() == "session_token"


def test_get_access_token_falls_back_to_env(auth, monkeypatch):
    monkeypatch.setenv("KITE_ACCESS_TOKEN", "env_token")
    from shared.config import get_settings
    get_settings.cache_clear()
    auth.cfg = get_settings()
    assert auth.get_access_token_sync() == "env_token"


def test_login_url_requires_api_key(monkeypatch):
    monkeypatch.delenv("KITE_API_KEY", raising=False)
    from shared.config import get_settings
    get_settings.cache_clear()
    service = KiteAuthService()
    with pytest.raises(ValueError, match="KITE_API_KEY"):
        service.login_url()


def test_login_url_builds(auth):
    with patch("kiteconnect.KiteConnect") as mock_kite:
        mock_kite.return_value.login_url.return_value = "https://kite.zerodha.com/connect/login?v=3"
        assert "kite.zerodha.com" in auth.login_url()


def test_complete_login_stores_session(auth):
    fake_data = {
        "access_token": "at_123",
        "refresh_token": "rt_456",
        "user_id": "AB1234",
        "user_name": "Test User",
    }
    mock_kite = MagicMock()
    mock_kite.generate_session.return_value = fake_data

    async def _run():
        async def noop_save():
            return None

        with patch("kiteconnect.KiteConnect", return_value=mock_kite):
            auth._save_to_db = noop_save  # type: ignore[method-assign]
            session = await auth.complete_login("req_token_xyz")
            assert session.access_token == "at_123"
            assert auth.get_access_token_sync() == "at_123"

    asyncio.run(_run())


def test_status_not_configured(monkeypatch):
    monkeypatch.delenv("KITE_API_KEY", raising=False)
    monkeypatch.delenv("KITE_API_SECRET", raising=False)
    from shared.config import get_settings
    get_settings.cache_clear()
    service = KiteAuthService()

    async def _run():
        status = await service.get_status()
        assert not status["configured"]
        assert not status["connected"]

    asyncio.run(_run())
