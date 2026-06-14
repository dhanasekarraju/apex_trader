"""Standard API response envelope."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ApiEnvelope(BaseModel):
    success: bool
    data: dict[str, Any] | list[Any] | None = None
    error: str = ""


def ok(data: Any = None) -> dict:
    if isinstance(data, dict):
        payload: dict | list | None = data
    elif isinstance(data, list):
        payload = data
    elif data is None:
        payload = {}
    else:
        payload = {"value": data}
    return {"success": True, "data": payload, "error": ""}


def fail(error: str, data: Any = None) -> dict:
    payload = data if isinstance(data, dict) else {}
    return {"success": False, "data": payload, "error": error}
