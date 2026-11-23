from __future__ import annotations

import pytest

from app.asgi import create_app
from app.security.internal_service_auth import InternalServicePrincipal


class FakeProductionAuthenticator:
    async def authenticate(self, request):
        return InternalServicePrincipal("web-bff")


def production_environment(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("OIDC_INTERNAL_AUTH_MODE", "injected")


def test_production_rejects_missing_injected_authenticator(monkeypatch):
    production_environment(monkeypatch)

    with pytest.raises(RuntimeError, match="not injected"):
        create_app()


def test_production_rejects_missing_or_wrong_auth_mode(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("OIDC_INTERNAL_AUTH_MODE", raising=False)

    with pytest.raises(RuntimeError, match="MODE=injected"):
        create_app()

    monkeypatch.setenv("OIDC_INTERNAL_AUTH_MODE", "local_shared_secret")

    with pytest.raises(RuntimeError, match="MODE=injected"):
        create_app()


def test_production_accepts_explicit_valid_adapter_injection(monkeypatch):
    production_environment(monkeypatch)

    app = create_app(
        internal_authentication=(
            FakeProductionAuthenticator(),
            frozenset({"web-bff"}),
        )
    )

    assert app is not None


@pytest.mark.parametrize(
    "authenticator,allowed_ids",
    [
        (None, frozenset({"web-bff"})),
        (FakeProductionAuthenticator(), frozenset()),
        (FakeProductionAuthenticator(), {"web-bff"}),
        (FakeProductionAuthenticator(), frozenset({""})),
        (FakeProductionAuthenticator(), frozenset({"x" * 129})),
    ],
)
def test_production_rejects_invalid_injected_configuration(
    monkeypatch,
    authenticator,
    allowed_ids,
):
    production_environment(monkeypatch)

    with pytest.raises(RuntimeError, match="configuration is invalid"):
        create_app(
            internal_authentication=(authenticator, allowed_ids)
        )


def test_injected_authentication_is_not_accepted_in_development(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("OIDC_INTERNAL_AUTH_MODE", raising=False)

    with pytest.raises(RuntimeError, match="only accepted in production"):
        create_app(
            internal_authentication=(
                FakeProductionAuthenticator(),
                frozenset({"web-bff"}),
            )
        )

@pytest.mark.parametrize(
    "malformed",
    [
        [],
        (FakeProductionAuthenticator(),),
        (FakeProductionAuthenticator(), frozenset({"web-bff"}), "extra"),
    ],
)
def test_production_rejects_malformed_authentication_tuple(monkeypatch, malformed):
    production_environment(monkeypatch)

    with pytest.raises(RuntimeError, match="configuration is invalid"):
        create_app(internal_authentication=malformed)


@pytest.mark.parametrize(
    "service_id",
    [
        " web-bff",
        "web-bff ",
        "web bff",
        "web-bff" + chr(10),
        "web-bff" + chr(9),
    ],
)
def test_production_rejects_noncanonical_service_ids(monkeypatch, service_id):
    production_environment(monkeypatch)

    with pytest.raises(RuntimeError, match="configuration is invalid"):
        create_app(
            internal_authentication=(
                FakeProductionAuthenticator(),
                frozenset({service_id}),
            )
        )
@pytest.mark.parametrize(
    "app_env",
    [None, "", "prod", "staging", "test", "unknown"],
)
def test_create_app_rejects_missing_or_unsupported_app_env(
    monkeypatch,
    app_env,
):
    if app_env is None:
        monkeypatch.delenv("APP_ENV", raising=False)
    else:
        monkeypatch.setenv("APP_ENV", app_env)

    with pytest.raises(
        RuntimeError,
        match="APP_ENV must be explicitly set",
    ):
        create_app()


def test_production_rejects_both_direct_injection_and_factory(
    monkeypatch,
):
    production_environment(monkeypatch)
    monkeypatch.setenv(
        "OIDC_INTERNAL_AUTH_FACTORY",
        "example_factory:build",
    )

    with pytest.raises(
        RuntimeError,
        match="either direct authentication injection or a factory",
    ):
        create_app(
            internal_authentication=(
                FakeProductionAuthenticator(),
                frozenset({"web-bff"}),
            )
        )