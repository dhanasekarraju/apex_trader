"""API envelope middleware and global exception handlers."""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from shared.api_response import fail, ok
from shared.logging import log_error


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, list):
            message = "; ".join(str(d) for d in detail)
        else:
            message = str(detail)
        return JSONResponse(fail(message), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            fail("Validation error", {"fields": exc.errors()}),
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        log_error(
            "unhandled_exception",
            path=str(request.url.path),
            error=str(exc),
        )
        return JSONResponse(fail("Internal server error"), status_code=500)


class EnvelopeMiddleware(BaseHTTPMiddleware):
    """Wrap JSON /api/* responses in {success, data, error}."""

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)

        response = await call_next(request)
        if response.status_code in range(300, 400):
            return response

        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type:
            return response

        body = b""
        async for chunk in response.body_iterator:
            body += chunk

        if not body:
            return JSONResponse(ok({}), status_code=response.status_code)

        try:
            payload: Any = json.loads(body)
        except json.JSONDecodeError:
            return Response(
                content=body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=content_type,
            )

        if isinstance(payload, dict) and "success" in payload:
            return JSONResponse(payload, status_code=response.status_code)

        if response.status_code >= 400:
            message = payload.get("detail", str(payload)) if isinstance(payload, dict) else str(payload)
            return JSONResponse(fail(str(message)), status_code=response.status_code)

        return JSONResponse(ok(payload), status_code=response.status_code)
