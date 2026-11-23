from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run_asgi_import(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    existing_pythonpath = env.get("PYTHONPATH", "")
    paths = [str(PROJECT_ROOT)]

    if existing_pythonpath:
        paths.append(existing_pythonpath)

    env["PYTHONPATH"] = os.pathsep.join(paths)

    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import app.asgi; assert app.asgi.app is not None",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def production_environment() -> dict[str, str]:
    env = os.environ.copy()
    env["APP_ENV"] = "production"
    env["OIDC_INTERNAL_AUTH_MODE"] = "injected"
    env.pop("OIDC_INTERNAL_SERVICE_SECRET", None)
    return env


def test_production_asgi_import_loads_configured_factory(tmp_path):
    factory_module = tmp_path / "bootstrap_factory.py"
    factory_module.write_text(
        """
class Authenticator:
    async def authenticate(self, request):
        return None

def build():
    return Authenticator(), frozenset({"web-bff"})
""".strip(),
        encoding="utf-8",
    )

    env = production_environment()
    env["OIDC_INTERNAL_AUTH_FACTORY"] = "bootstrap_factory:build"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(tmp_path), str(PROJECT_ROOT)]
    )

    result = run_asgi_import(env)

    assert result.returncode == 0, (
        f"Production ASGI import failed.\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_production_asgi_import_fails_when_factory_is_missing():
    env = production_environment()
    env.pop("OIDC_INTERNAL_AUTH_FACTORY", None)

    result = run_asgi_import(env)

    assert result.returncode != 0
    assert "adapter is not injected" in result.stderr


def test_production_asgi_import_fails_closed_for_missing_factory_module():
    env = production_environment()
    env["OIDC_INTERNAL_AUTH_FACTORY"] = (
        "module_that_does_not_exist_for_zak_bootstrap:build"
    )

    result = run_asgi_import(env)

    assert result.returncode != 0
    assert "factory could not be loaded" in result.stderr


def test_development_asgi_import_rejects_production_factory():
    env = os.environ.copy()
    env["APP_ENV"] = "development"
    env["OIDC_INTERNAL_AUTH_MODE"] = ""
    env["OIDC_INTERNAL_AUTH_FACTORY"] = "bootstrap_factory:build"
    env.pop("OIDC_INTERNAL_SERVICE_SECRET", None)

    result = run_asgi_import(env)

    assert result.returncode != 0
    assert "only accepted in production" in result.stderr


def configure_real_hmac_factory_environment(env: dict[str, str]) -> None:
    import base64

    secret = bytes(range(32))
    encoded_secret = base64.urlsafe_b64encode(secret).decode("ascii").rstrip("=")

    env["OIDC_INTERNAL_AUTH_FACTORY"] = (
        "app.security.hmac_internal_service_auth_factory:"
        "build_internal_service_authentication"
    )
    env["OIDC_INTERNAL_SERVICE_ID"] = "web-bff"
    env["OIDC_INTERNAL_SERVICE_HMAC_ACTIVE_KEY_ID"] = "key-bootstrap-2026"
    env["OIDC_INTERNAL_SERVICE_HMAC_KEYS"] = (
        '{"key-bootstrap-2026":"' + encoded_secret + '"}'
    )


def test_production_asgi_import_loads_real_hmac_factory():
    env = production_environment()
    configure_real_hmac_factory_environment(env)

    result = run_asgi_import(env)

    assert result.returncode == 0, (
        f"Real HMAC production ASGI import failed.\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_production_asgi_import_fails_closed_when_hmac_config_is_missing():
    env = production_environment()
    configure_real_hmac_factory_environment(env)
    env.pop("OIDC_INTERNAL_SERVICE_HMAC_KEYS")

    result = run_asgi_import(env)

    assert result.returncode != 0
    assert "factory could not be loaded" in result.stderr


def test_production_asgi_import_fails_closed_when_active_hmac_key_is_unknown():
    env = production_environment()
    configure_real_hmac_factory_environment(env)
    env["OIDC_INTERNAL_SERVICE_HMAC_ACTIVE_KEY_ID"] = "unknown-active-key"

    result = run_asgi_import(env)

    assert result.returncode != 0
    assert "factory could not be loaded" in result.stderr
