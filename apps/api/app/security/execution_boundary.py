"""Fail-closed execution-mode policy; no exchange or paper execution adapter."""
from enum import Enum


class UnsupportedExecutionMode(ValueError):
    """Raised when a requested execution mode is not explicitly permitted."""


class ExecutionMode(str, Enum):
    """Execution mode currently permitted by the application boundary."""
    PAPER = "paper"


def require_supported_execution_mode(value: str | ExecutionMode) -> ExecutionMode:
    """Accept only internal paper mode; reject unknown modes fail-closed."""
    if isinstance(value, ExecutionMode):
        return value
    if not isinstance(value, str):
        raise UnsupportedExecutionMode("Execution mode must be a supported string.")
    try:
        return ExecutionMode(value.strip().lower())
    except ValueError as exc:
        raise UnsupportedExecutionMode(
            "Unsupported execution mode. Only internal paper mode is permitted."
        ) from exc
