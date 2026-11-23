from __future__ import annotations

import sys
from types import ModuleType

import pytest

from app.security.internal_service_auth_factory import (
    load_internal_authentication_factory,
)


class FakeAuthenticator:
    async def authenticate(self, request):
        return None


def test_factory_loads_and_returns_authentication_tuple(monkeypatch):
    module = ModuleType("test_production_auth_factory")
    expected = (FakeAuthenticator(), frozenset({"web-bff"}))
    module.build = lambda: expected
    monkeypatch.setitem(sys.modules, module.__name__, module)

    result = load_internal_authentication_factory(
        "test_production_auth_factory:build"
    )

    assert result is expected


@pytest.mark.parametrize(
    "spec",
    [
        "",
        "missing-colon",
        ":factory",
        "module:",
        "module:factory:extra",
        "module-name:factory",
        "module.factory",
        "module:factory-name",
    ],
)
def test_factory_rejects_malformed_specification(spec):
    with pytest.raises(RuntimeError, match="configuration is invalid"):
        load_internal_authentication_factory(spec)


def test_factory_rejects_missing_module():
    with pytest.raises(RuntimeError, match="could not be loaded"):
        load_internal_authentication_factory(
            "module_that_does_not_exist_for_zak:build"
        )


def test_factory_rejects_missing_callable():
    module = ModuleType("test_factory_without_callable")
    sys.modules[module.__name__] = module
    try:
        with pytest.raises(RuntimeError, match="could not be loaded"):
            load_internal_authentication_factory(
                "test_factory_without_callable:build"
            )
    finally:
        sys.modules.pop(module.__name__, None)


def test_factory_rejects_factory_exception(monkeypatch):
    module = ModuleType("test_factory_raises")

    def fail():
        raise ValueError("sensitive internal detail")

    module.build = fail
    monkeypatch.setitem(sys.modules, module.__name__, module)

    with pytest.raises(RuntimeError, match="could not be loaded") as error:
        load_internal_authentication_factory("test_factory_raises:build")

    assert "sensitive internal detail" not in str(error.value)


@pytest.mark.parametrize("result", [None, [], {}, ("only-one",)])
def test_factory_rejects_invalid_return_shape(monkeypatch, result):
    module = ModuleType("test_factory_invalid_return")
    module.build = lambda: result
    monkeypatch.setitem(sys.modules, module.__name__, module)

    with pytest.raises(RuntimeError, match="returned invalid configuration"):
        load_internal_authentication_factory(
            "test_factory_invalid_return:build"
        )
