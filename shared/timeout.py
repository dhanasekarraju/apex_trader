"""Async timeout helpers for external I/O."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


async def with_timeout(
    coro: Awaitable[T],
    *,
    seconds: float,
    label: str = "operation",
) -> T:
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except asyncio.TimeoutError as exc:
        raise TimeoutError(f"{label} timed out after {seconds}s") from exc


async def run_in_executor_with_timeout(
    loop,
    fn: Callable[..., T],
    *args,
    seconds: float,
    label: str = "operation",
    **kwargs,
) -> T:
    return await with_timeout(
        loop.run_in_executor(None, lambda: fn(*args, **kwargs)),
        seconds=seconds,
        label=label,
    )
