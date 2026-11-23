"""Trusted deployment-configured production authenticator factory loader."""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable

_FACTORY_SPEC = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:"
    r"[A-Za-z_][A-Za-z0-9_]*$"
)


def load_internal_authentication_factory(spec: str) -> tuple[object, object]:
    """Load and invoke a trusted deployment factory.

    The factory path must come from protected server-side deployment
    configuration. It is never accepted from a request or user-controlled data.
    """
    if not isinstance(spec, str) or not _FACTORY_SPEC.fullmatch(spec):
        raise RuntimeError("Production authentication factory configuration is invalid.")

    module_name, function_name = spec.split(":", 1)

    try:
        module = importlib.import_module(module_name)
        factory = getattr(module, function_name, None)

        if not callable(factory):
            raise RuntimeError("Configured authentication factory is unavailable.")

        result = factory()

    except Exception:
        raise RuntimeError(
            "Production authentication factory could not be loaded."
        ) from None

    if not isinstance(result, tuple) or len(result) != 2:
        raise RuntimeError(
            "Production authentication factory returned invalid configuration."
        )

    return result
