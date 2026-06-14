"""Execution router — delegates to central execution engine (backward compatible)."""

from services.execution.execution_engine import ExecutionEngine, ExecutionRouter

__all__ = ["ExecutionEngine", "ExecutionRouter"]
