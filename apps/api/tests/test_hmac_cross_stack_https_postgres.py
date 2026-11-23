"""Real HTTPS integration: TypeScript HMAC transport -> FastAPI -> PostgreSQL."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.security.hmac_internal_service_auth import (
    HMACInternalServiceAuthenticator,
)
from app.security.internal_service_auth import (
    InternalServicePrincipal,
    get_internal_service_principal,
)


NODE_SCRIPT = r"""

const path = require("node:path");
const fs = require("node:fs");
const ts = require("typescript");

require.extensions[".ts"] = function(module, filename) {
  const source = fs.readFileSync(filename, "utf8");
  const result = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      esModuleInterop: true
    },
    fileName: filename
  });
  module._compile(result.outputText, filename);
};

async function main() {
  const webRoot = process.env.ZAK_TEST_WEB_ROOT;
  const transportPath = path.join(
    webRoot, "src/lib/server/hmac-internal-api-transport.ts"
  );
  const { createHmacInternalServiceTransport } = require(transportPath);
  const serviceId = process.env.ZAK_TEST_SERVICE_ID;
  const keyId = process.env.ZAK_TEST_KEY_ID;
  const secret = Buffer.from(process.env.ZAK_TEST_SECRET_B64URL, "base64url");
  const origin = process.env.ZAK_TEST_ALLOWED_ORIGIN;

  const transport = createHmacInternalServiceTransport({
    serviceId, activeKeyId: keyId, allowedOrigin: origin,
    keys: new Map([[keyId, secret]])
  });
  const originalFetch = globalThis.fetch;

  async function send(testCase) {
    let captured = null;
    globalThis.fetch = async (input, init) => {
      const url = new URL(input instanceof URL ? input.href : String(input));
      const headers = new Headers(init.headers);
      captured = {
        url: url.href,
        init: { ...init, headers: new Headers(headers) }
      };

      switch (testCase) {
        case "body_tampering":
          init = { ...init, body: String(init.body) + " " };
          break;
        case "path_tampering":
          url.pathname += "/alternate";
          break;
        case "query_tampering":
          url.searchParams.set("check", "tampered");
          break;
        case "invalid_signature":
          headers.set("X-Zak-Internal-Signature", "0".repeat(64));
          break;
        case "wrong_service_id":
          headers.set("X-Zak-Internal-Service-Id", "wrong-service");
          break;
        case "unknown_key_id":
          headers.set("X-Zak-Internal-Key-Id", "unknown-key");
          break;
        case "stale_timestamp":
          headers.set("X-Zak-Internal-Timestamp",
            String(Math.floor(Date.now()/1000)-301));
          break;
        case "future_timestamp":
          headers.set("X-Zak-Internal-Timestamp",
            String(Math.floor(Date.now()/1000)+301));
          break;
      }

      return originalFetch(url.href, { ...init, headers });
    };

    try {
      const response = await transport.fetch(
        new URL(origin + "/cross-stack-auth-probe?check=1&check=2"),
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ source: "typescript", value: 42 })
        }
      );
      return { response, captured };
    } finally {
      globalThis.fetch = originalFetch;
    }
  }

  const first = await send("valid");
  if (!first.captured) throw new Error("Valid request was not captured.");
  const firstBody = await first.response.json();
  const nonce = first.captured.init.headers.get("X-Zak-Internal-Nonce");
  const replay = await originalFetch(first.captured.url, first.captured.init);
  await replay.arrayBuffer();

  const names = [
    "body_tampering", "path_tampering", "query_tampering",
    "invalid_signature", "wrong_service_id", "unknown_key_id",
    "stale_timestamp", "future_timestamp"
  ];
  const negativeResults = [];

  for (const name of names) {
    const result = await send(name);
    if (!result.captured) throw new Error("Request not captured: " + name);
    negativeResults.push({
      name,
      status: result.response.status,
      nonce: result.captured.init.headers.get("X-Zak-Internal-Nonce")
    });
    await result.response.arrayBuffer();
  }

  process.stdout.write(JSON.stringify({
    firstStatus: first.response.status,
    firstBody,
    replayStatus: replay.status,
    nonce,
    keyId,
    negativeResults
  }) + "\n");
}

main().catch(error => {
  process.stderr.write(String(error && error.stack || error) + "\n");
  process.exitCode = 1;
});

"""


def _write_test_certificate(directory: Path) -> tuple[Path, Path, Path]:
    """Create a temporary private CA and localhost server certificate."""
    now = datetime.now(timezone.utc)

    ca_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    ca_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "ZaK Integration Test CA"),
    ])

    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=2))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=0),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    server_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    server_name = x509.Name([
        x509.NameAttribute(
            NameOID.COMMON_NAME,
            "127.0.0.1",
        ),
    ])

    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=2))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            ]),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([
                ExtendedKeyUsageOID.SERVER_AUTH,
            ]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    ca_path = directory / "test-ca.pem"
    cert_path = directory / "server-cert.pem"
    key_path = directory / "server-key.pem"

    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        server_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    return ca_path, cert_path, key_path


def _run_uvicorn_selector(server: uvicorn.Server) -> None:
    """Run the test HTTPS server on a dedicated Windows-compatible selector loop."""
    loop = asyncio.SelectorEventLoop()
    asyncio.set_event_loop(loop)

    try:
        loop.run_until_complete(server.serve())
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()
            asyncio.set_event_loop(None)

class _DatabaseManagerContract:
    """Expose the same session_factory() contract used by the verifier."""

    def __init__(self, sessions):
        self._sessions = sessions

    def session_factory(self):
        return self._sessions


@pytest.mark.asyncio
async def test_real_typescript_https_hmac_authentication_and_postgres_replay():
    database_url = os.environ.get("DATABASE_URL")
    assert database_url, "DATABASE_URL must be provided by the guarded runner."

    parsed = make_url(database_url)
    assert parsed.drivername == "postgresql+asyncpg"
    assert parsed.host == "127.0.0.1"
    assert parsed.port == 55432
    assert parsed.database == "zak_oidc_test"
    assert parsed.username == "zak_oidc_test_app"

    engine = create_async_engine(
        database_url,
        poolclass=NullPool,
    )
    sessions = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )

    service_id = "cross-stack-" + uuid.uuid4().hex
    key_id = "cross-stack-test-key"
    secret = os.urandom(32)
    nonce_hash = None
    server = None
    server_thread = None
    identity_verified = False

    try:
        async with engine.connect() as connection:
            identity = (
                await connection.execute(text("""
                    SELECT
                        current_user,
                        current_database(),
                        current_setting('port'),
                        current_setting('server_version_num')
                """))
            ).one()

        assert tuple(str(value) for value in identity) == (
            "zak_oidc_test_app",
            "zak_oidc_test",
            "55432",
            "180006",
        ), "Refusing to run against an unapproved PostgreSQL target."

        identity_verified = True

        authenticator = HMACInternalServiceAuthenticator(
            service_id=service_id,
            keys={key_id: secret},
        )

        app = FastAPI()
        app.state.internal_service_authenticator = authenticator
        app.state.internal_service_allowed_ids = frozenset({service_id})
        app.state.database_manager = _DatabaseManagerContract(sessions)

        @app.post("/cross-stack-auth-probe")
        @app.post("/cross-stack-auth-probe/alternate")
        async def authenticated_probe(
            principal: InternalServicePrincipal = Depends(
                get_internal_service_principal
            ),
        ):
            return {"service_id": principal.service_id}

        with tempfile.TemporaryDirectory(
            prefix="zak-hmac-cross-stack-"
        ) as temporary_directory:
            temp = Path(temporary_directory)
            ca_path, cert_path, key_path = _write_test_certificate(temp)

            # Reserve an ephemeral loopback port, then release it for Uvicorn.
            import socket

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]

            origin = f"https://127.0.0.1:{port}"

            config = uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="critical",
                access_log=False,
                lifespan="off",
                ssl_certfile=str(cert_path),
                ssl_keyfile=str(key_path),
            )
            server = uvicorn.Server(config)
            server_thread = threading.Thread(
                target=_run_uvicorn_selector,
                args=(server,),
                name="zak-hmac-cross-stack-https",
                daemon=True,
            )
            server_thread.start()

            deadline = time.monotonic() + 15
            while not server.started:
                if not server_thread.is_alive():
                    raise RuntimeError("HTTPS test server stopped before startup.")
                if time.monotonic() >= deadline:
                    raise TimeoutError("HTTPS test server startup timed out.")
                await asyncio.sleep(0.05)

            api_root = Path(__file__).resolve().parents[1]
            repo_root = api_root.parent.parent
            web_root = repo_root / "apps" / "web"

            environment = os.environ.copy()
            environment.update({
                "NODE_EXTRA_CA_CERTS": str(ca_path),
                "ZAK_TEST_WEB_ROOT": str(web_root),
                "ZAK_TEST_SERVICE_ID": service_id,
                "ZAK_TEST_KEY_ID": key_id,
                "ZAK_TEST_SECRET_B64URL": (
                    __import__("base64")
                    .urlsafe_b64encode(secret)
                    .decode("ascii")
                    .rstrip("=")
                ),
                "ZAK_TEST_ALLOWED_ORIGIN": origin,
            })

            node = subprocess.run(
                [
                    "node",
                    "--conditions=react-server",
                    "-e",
                    NODE_SCRIPT,
                ],
                cwd=web_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

            assert node.returncode == 0, (
                "Node HMAC transport process failed.\n"
                f"stdout: {node.stdout}\n"
                f"stderr: {node.stderr}"
            )

            output_lines = [
                line for line in node.stdout.splitlines()
                if line.strip()
            ]
            assert output_lines, "Node process returned no result."
            result = json.loads(output_lines[-1])

            assert result["firstStatus"] == 200
            assert result["firstBody"] == {"service_id": service_id}
            assert result["replayStatus"] == 401
            assert result["nonce"]
            assert len(result["nonce"]) == 43
            assert result["keyId"] == key_id

            expected = {
                "body_tampering", "path_tampering", "query_tampering",
                "invalid_signature", "wrong_service_id", "unknown_key_id",
                "stale_timestamp", "future_timestamp",
            }
            negatives = result["negativeResults"]
            assert {item["name"] for item in negatives} == expected

            for item in negatives:
                assert item["status"] == 401, (
                    f"{item['name']} returned {item['status']}, expected 401"
                )
                assert item["nonce"] and len(item["nonce"]) == 43

            success_hash = hashlib.sha256(
                result["nonce"].encode("ascii")
            ).hexdigest()
            negative_hashes = [
                hashlib.sha256(item["nonce"].encode("ascii")).hexdigest()
                for item in negatives
            ]
            assert len(set(negative_hashes)) == len(negative_hashes)

            async with sessions() as session:
                success_count = await session.scalar(
                    text("""
                        SELECT count(*) FROM internal_service_request_nonces
                        WHERE service_id = :service_id
                          AND key_id = :key_id
                          AND nonce_hash = :nonce_hash
                    """),
                    {
                        "service_id": service_id,
                        "key_id": key_id,
                        "nonce_hash": success_hash,
                    },
                )
                rejected_counts = []
                for nonce_hash in negative_hashes:
                    count = await session.scalar(
                        text("""
                            SELECT count(*) FROM internal_service_request_nonces
                            WHERE service_id = :service_id
                              AND key_id = :key_id
                              AND nonce_hash = :nonce_hash
                        """),
                        {
                            "service_id": service_id,
                            "key_id": key_id,
                            "nonce_hash": nonce_hash,
                        },
                    )
                    rejected_counts.append(count)

            assert success_count == 1
            assert rejected_counts == [0] * len(expected), (
                f"Rejected request nonces persisted: {rejected_counts}"
            )

    finally:
        try:
            if server is not None:
                server.should_exit = True

            if server_thread is not None:
                await asyncio.to_thread(server_thread.join, 10)
                assert not server_thread.is_alive(), (
                    "HTTPS integration server did not shut down."
                )
        finally:
            try:
                if identity_verified:
                    async with sessions() as session:
                        async with session.begin():
                            await session.execute(
                                text("""
                                    DELETE FROM internal_service_request_nonces
                                    WHERE service_id = :service_id
                                """),
                                {"service_id": service_id},
                            )
            finally:
                await engine.dispose()