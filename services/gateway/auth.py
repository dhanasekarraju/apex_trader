"""API authentication — all mutating trading endpoints require a valid key."""

from __future__ import annotations

import hashlib
import secrets

from fastapi import HTTPException, Security, WebSocket, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from shared.config import Settings, get_settings

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_bearer = HTTPBearer(auto_error=False)


def resolve_api_access_key(cfg: Settings | None = None) -> str:
    """Production key from API_ACCESS_KEY; dev fallback derived from SECRET_KEY."""
    cfg = cfg or get_settings()
    if cfg.api_access_key.strip():
        return cfg.api_access_key.strip()
    digest = hashlib.sha256(cfg.secret_key.encode()).hexdigest()
    return digest[:32]


def _extract_token(
    x_api_key: str | None,
    credentials: HTTPAuthorizationCredentials | None,
    query_api_key: str | None = None,
) -> str | None:
    if x_api_key:
        return x_api_key.strip()
    if credentials and credentials.scheme.lower() == "bearer":
        return credentials.credentials.strip()
    if query_api_key:
        return query_api_key.strip()
    return None


def verify_api_token(token: str | None, cfg: Settings | None = None) -> None:
    expected = resolve_api_access_key(cfg)
    if not token or not secrets.compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized — valid API key required",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def require_api_auth(
    x_api_key: str | None = Security(_api_key_header),
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> None:
    verify_api_token(_extract_token(x_api_key, credentials))


async def require_ws_auth(ws: WebSocket) -> None:
    token = ws.query_params.get("token") or ws.headers.get("x-api-key")
    verify_api_token(token)


def cors_allowed_origins(cfg: Settings | None = None) -> list[str]:
    cfg = cfg or get_settings()
    raw = cfg.cors_allowed_origins.strip()
    if raw:
        return [origin.strip() for origin in raw.split(",") if origin.strip()]
    base = cfg.public_url.strip().rstrip("/")
    if base.startswith("http"):
        return [base]
    return ["http://127.0.0.1:8080", "http://localhost:8080"]
