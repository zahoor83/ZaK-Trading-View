# OIDC Browser Authentication Security Audit

**Project:** ZaK Trading View  
**Audit scope:** Browser authentication, OIDC integration, session lifecycle and related backend security  
**Review date:** 2026-11-13  
**Status:** Source review completed; documented limitations remain

## 1. Executive Summary

The reviewed implementation provides a server-side OIDC browser authentication flow using a Next.js backend-for-frontend (BFF), a FastAPI internal API, Auth0 as the configured identity provider, and PostgreSQL-backed authentication transactions and sessions.

The reviewed source and supplied test evidence demonstrate controls for:

- OIDC state, nonce, browser binding and PKCE.
- Authorization transaction single-use consumption.
- ID-token signature and claims validation.
- Session token hashing, expiry and revocation.
- Browser cookie security.
- Exact Origin validation and CSRF defenses.
- Internal service authentication and fail-closed behavior.
- Idempotent login initiation and encrypted replay responses.
- Bounded provider HTTP responses and restricted HTTP client configuration.

This is a source-and-test-evidence audit. It is not an independent penetration test, production certification or proof of live Auth0 deployment security.

## 2. Scope and Method

The review covered the supplied source inspections, test outputs, migration inspection and project verification results for Section 4 and Section 5.

Reviewed backend areas included:

- `app/asgi.py`
- `app/api/internal_auth.py`
- `app/api/internal_auth_callbacks.py`
- `app/security/internal_service_auth.py`
- `app/security/local_shared_secret_auth.py`
- `app/security/oidc_configuration.py`
- `app/security/oidc_runtime.py`
- `app/security/oidc_token_verification.py`
- `app/security/oidc_http.py`
- `app/security/session_authentication.py`
- `app/services/oidc_auth_service.py`
- `app/services/oidc_login_orchestration.py`
- `app/services/oidc_transaction_service.py`
- `app/services/oidc_idempotency_service.py`
- `app/services/oidc_idempotency_encryption.py`
- `app/services/session_service.py`
- `app/models/identity.py`
- `app/models/oidc.py`
- `migrations/versions/0003_oidc_auth_transactions.py`
- `migrations/versions/0004_oidc_idempotency_records.py`

Frontend areas reviewed included:

- Login, callback and logout route handlers.
- Server-side internal API client.
- Session cookie parsing and validation.
- Protected route layout and session context.
- Browser authentication E2E tests.

## 3. Main Security Findings

### 3.1 OIDC transaction security

The reviewed implementation stores hashes of state, nonce and browser-binding values rather than their raw values.

The PKCE verifier is encrypted before persistence. Transaction consumption uses a database row lock and marks the transaction consumed before returning the verifier. The transaction is short-lived and single-use.

Tests cover replay, expiry, unknown state, incorrect browser binding, invalid transaction state and corrupted encrypted data.

### 3.2 ID-token validation

The reviewed verifier enforces RS256 and validates issuer, audience, authorized party where required, expiry, issued-at time, not-before time, subject and nonce.

The implementation rejects token-directed key references and unsupported signing algorithms. JWKS refresh behavior is bounded and unknown-key refresh is rate limited.

Tests cover invalid signatures, wrong claims, invalid algorithms, unknown key IDs, malformed tokens, JWKS failures and private key material rejection.

### 3.3 Identity provisioning

`AuthIdentity` enforces a unique `(issuer, subject)` identity.

The reviewed provisioning service requires a verified email and rejects automatic linking when an existing email belongs to a different provider identity. The reviewed service also handles a concurrent identity creation race by checking the exact provider identity after an integrity conflict.

### 3.4 Session lifecycle

The browser receives an opaque session token. The backend stores a SHA-256 token hash rather than the raw token.

The session service checks user eligibility, expiry and revocation. Revocation is scoped to the authenticated user and session ownership.

The browser cookie uses the `__Host-` prefix, HttpOnly, Secure, SameSite=Lax and Path=/. The callback does not expose the session token in the browser URL.

### 3.5 CSRF, Origin and redirects

The reviewed login and logout routes validate the configured public origin and reject cross-origin requests. Fetch Metadata is checked when present.

Callback state and browser-binding checks are enforced. Callback and logout redirects use fixed same-origin destinations rather than caller-controlled redirect targets.

The BFF internal fetch uses `redirect: "error"`.

### 3.6 Internal service authentication

The internal API requires an authenticated service principal and checks the service allowlist.

Missing or invalid credentials are rejected. Unavailable authentication configuration fails closed. The shared-secret comparison uses constant-time comparison.

The session validation dependency order was tested so that unauthorized internal service requests are rejected before database access.

### 3.7 OIDC HTTP transport

The production OIDC client factory explicitly configures:

- TLS verification enabled.
- Environment proxy inheritance disabled.
- Redirect following disabled.
- Explicit proxy disabled.
- Custom mounts excluded.
- No caller-supplied transport.

Both inspected production call paths use this factory:

- Provider discovery and JWKS retrieval in `oidc_configuration.py`.
- Authorization-code token exchange in `oidc_auth_service.py`.

The bounded-response helper enforces a maximum body size, a total deadline, identity-only content encoding and redirect rejection.

The helper checks `trust_env=False` on a supplied client. That check alone is not a complete trust validation mechanism for arbitrary clients. The reviewed production call sites create their clients through the restricted factory and do not inject arbitrary clients. Future callers must preserve this trusted-client boundary.

### 3.8 Database integrity

The inspected models and migrations agree on the OIDC transaction and idempotency table structures.

The migrations include checks for hash formats, encryption nonce and ciphertext lengths, expiry relationships and valid status values.

The idempotency record uses a unique service/key-hash constraint and a nullable transaction foreign key with `ON DELETE SET NULL`.

## 4. Verification Evidence

The following results were supplied from user-executed commands:

| Verification | Result |
|---|---|
| Backend non-PostgreSQL regression | 1,042 passed; 466 subtests passed |
| Isolated PostgreSQL OIDC integration | 8 passed |
| Combined backend coverage | 1,050 tests passed across the two runs |
| OIDC transaction/token/auth focused tests | 91 passed |
| Internal service authorization targeted tests | 35 passed |
| Browser authentication E2E | 22 passed |
| Frontend typecheck | Passed |
| Frontend production build | Passed |
| Actual isolated database Alembic revision | `0004_oidc_idempotency_records (head)` |
| OIDC HTTP tests | Included in the supplied full backend regression; separate result not recorded in this report |

The PostgreSQL integration tests used the isolated test cluster on `127.0.0.1:55432`, database `zak_oidc_test`, and role `zak_oidc_test_app`.

The isolated PostgreSQL test runner restored process environment variables after execution. The separate PostgreSQL service on port 5432 was not used.

## 5. Test Environment and Limitations

The browser E2E tests use mocked Auth0 and mocked internal API behavior. They do not establish successful authentication against a live Auth0 tenant.

The following were not established by this review:

- Live Auth0 tenant integration.
- Deployed production environment behavior.
- Independent external penetration testing.
- OS-wide process or domain isolation.
- Production secret-management deployment and rotation verification.
- Production operational monitoring and incident response readiness.
- Full production infrastructure security certification.

OIDC browser routes remain development-only under the reviewed configuration. Production enablement is not approved by this audit.

The historical Part 3 signal-only acceptance documents are preserved and are not replaced by this OIDC audit.

## 6. Residual Risks and Operational Requirements

Before production enablement:

1. Complete an approved production OIDC configuration and deployment design.
2. Define production internal-service authentication and secret/key lifecycle management.
3. Verify TLS, reverse-proxy and network boundaries in the deployed environment.
4. Perform live provider integration tests in a controlled environment.
5. Arrange independent security testing appropriate to the deployment risk.
6. Review logging, monitoring, alerting and incident response controls.
7. Re-run the complete regression and security test suites against the approved release candidate.

## 7. Audit Conclusion

The inspected source and supplied test evidence support acceptance of the reviewed development-scope browser authentication implementation, subject to the limitations documented above.

This conclusion must not be interpreted as production security certification or approval to enable the OIDC routes in production.


## 8. Latest Browser E2E Evidence — 2026-11-14

The latest user-executed full browser authentication regression completed with 22/22 tests passing using Chromium, one worker and a 120,000 ms command-line test timeout override.

The earlier isolated unauthenticated redirect test also passed with the extended timeout. This supports a test-environment timing explanation for the earlier 60-second timeout, but does not independently prove that all browser environments will have the same performance.

Auth0 CDN assets and Google Fonts generated 
et::ERR_ABORTED messages during the mocked provider flow. The test suite passed, but live Auth0 tenant connectivity remains unverified. The permanent Playwright configuration was not changed.
## 9. Section 5 Final Evidence Reconciliation — 2026-11-14

- The Section 5 acceptance document and security audit report were inspected together.
- The source-review and documentation activities listed under Task 2G were already recorded as completed.
- The latest full browser authentication regression passed 22/22 with a 120,000 ms command-line timeout override.
- The inconsistent pending checklist status was reconciled with the documented development-scope final decision.
- Section 5 Task 2G is complete for the reviewed development scope.
- This reconciliation does not certify production security, establish live Auth0 connectivity or approve production OIDC enablement.


## 10. Section 4 OIDC BFF Transport Implementation — 2026-11-14

### 10.1 Reviewed frontend implementation

The following frontend components were implemented and reviewed:

- `src/instrumentation.ts`
- `src/lib/server/internal-api-transport.ts`
- `src/lib/server/internal-api-transport-factory.ts`
- `src/lib/server/internal-api.ts`

The production bootstrap loads the configured factory, registers the resulting transport and validates the registered transport before normal application operation.

The development path continues to use the development-only local transport. The production path requires a separately injected factory result and does not fall back to the local shared-secret adapter.

### 10.2 Latest user-executed evidence

| Verification | Result |
|---|---|
| Factory loader tests | 6/6 passed |
| Combined transport regression | 17/17 passed |
| TypeScript typecheck | Passed |
| Next.js production build | Passed |
| Valid test-factory production startup | Passed |
| Missing factory configuration | Rejected |
| Invalid factory configuration | Rejected |
| Tested routes with invalid factory | HTTP 500 on all six tested paths; no 2xx |
| Final missing-factory listener audit | No listener; no HTTP response |
| Test cleanup | Port 3199 verified free |

The six tested paths in the invalid-factory run were `/`, `/login`, `/api/market-data/health`, `/api/auth/logout`, `/api/auth/oidc/login` and `/api/market-data/latest/spot/BTCUSDT`.

### 10.3 Fail-closed behavior qualification

Next.js logged instrumentation and preload failures for missing and invalid production factory configuration. The process temporarily remained alive and tested requests returned HTTP 500. A later process/listener audit found no listener and no HTTP response.

This is recorded as invalid-configuration rejection and tested-route failure, not as proof of immediate process termination or OS-wide isolation.

### 10.4 Section 4 implementation checklist

| ID | Phase | Development-scope status |
|---|---|---|
| 1F-D | OIDC scope and documentation verification | COMPLETE |
| 1G | Browser login initiation | COMPLETE |
| 1H | OIDC callback and authorization-code exchange | COMPLETE |
| 1I | Browser session cookie issuance and secure handling | COMPLETE |
| 1J | Browser logout and session termination | COMPLETE |
| 1K | CSRF protection and exact Origin validation | COMPLETE |
| 1L | Next.js BFF to FastAPI private service authentication | COMPLETE — provider-neutral bootstrap; production adapter pending |
| 1M | Browser-facing session validation and revocation | COMPLETE |

### 10.5 Production deployment limitations

The hosting provider and production service-authentication protocol have not been selected. No real production adapter or authenticated production FastAPI request was verified.

Production adapter implementation, secret/key lifecycle, deployed network/TLS boundaries, live provider integration and independent deployment security review remain prerequisites.

This implementation review does not approve production OIDC enablement and does not constitute production security certification.


## 11. FastAPI HMAC Production Bootstrap Verification — 2026-11-15

### 11.1 Verified backend factory integration

The FastAPI production ASGI bootstrap was verified against the actual
HMAC authentication factory:

pp.security.hmac_internal_service_auth_factory:build_internal_service_authentication

The backend deployment configuration uses:

- OIDC_INTERNAL_AUTH_FACTORY to select the trusted server-side factory.
- OIDC_INTERNAL_SERVICE_ID to identify the internal service.
- OIDC_INTERNAL_SERVICE_HMAC_ACTIVE_KEY_ID to select the active key.
- OIDC_INTERNAL_SERVICE_HMAC_KEYS to provide the configured key ring.

The factory loader imports and invokes the configured callable. The ASGI
bootstrap validates the returned authenticator and service allowlist.

### 11.2 User-executed verification evidence

| Verification | Result |
|---|---|
| Python syntax compilation of bootstrap test file | Passed |
| ASGI bootstrap test suite | 7/7 passed |
| Real HMAC factory production ASGI import | Passed |
| Missing HMAC key-ring configuration | Startup rejected |
| Unknown active HMAC key ID | Startup rejected |
| Test collection | 7 tests collected |

The successful bootstrap used test-only key material. No production
secret was displayed or provisioned by this test.

### 11.3 Scope and limitations

This verifies the Python backend factory-to-ASGI bootstrap path and its
tested fail-closed configuration cases.

It does not verify a deployed production environment, production secret
manager integration, live key provisioning or rotation, a live authenticated
Next.js-to-FastAPI production request, or OS-wide process isolation.

The Next.js OIDC_INTERNAL_TRANSPORT_FACTORY evidence recorded in Section 10
is a separate frontend bootstrap and is not replaced by this backend result.

Production OIDC enablement remains NOT APPROVED pending deployment-specific
security design and validation.

## 12. HMAC Full Regression and Security Acceptance — 2026-11-15

### 12.1 Final user-executed regression evidence

The following results were supplied from the user's local Windows development
environment. Test execution was performed locally; this record does not claim
that the assistant executed commands on the user's machine.

| Verification | Result |
|---|---|
| Non-PostgreSQL backend regression | 1,210 passed; 466 subtests passed |
| Guarded PostgreSQL integration regression | 13 passed |
| PostgreSQL test database identity | zak_oidc_test_app / zak_oidc_test / port 55432 / PostgreSQL 180006 |
| HMAC TypeScript transport regression | 30 passed in 4.9 seconds |
| Frontend TypeScript typecheck | Passed |
| Next.js 16.4.4 production build | Passed |
| OIDC browser E2E regression | 29 passed in approximately 2.5 minutes |

The guarded PostgreSQL runner restored its process environment after execution.
It did not request migrations or schema changes. Its verified target was the
dedicated test database on port 55432, not the default PostgreSQL port 5432.

### 12.2 Playwright server-only boundary

The Playwright test runner required the Node `react-server` export condition
to discover and execute tests importing Next.js server-only modules.

The condition was supplied to the test-runner process only. The production
`server-only` imports and application source files were not removed or
weakened as part of this workaround.

The HMAC transport suite discovered and executed 30 tests across three files.
The browser E2E suite executed 29 tests using the configured local mock API
and Next.js production server.

### 12.3 Development-scope acceptance

The combined backend, dedicated PostgreSQL, HMAC transport, frontend typecheck,
production build and browser E2E evidence supports completion of the reviewed
development implementation and its tested regression scope.

Section 1L-16 is COMPLETE for this development-scope regression and
documentation acceptance.

### 12.4 Explicit production limitations

The browser E2E environment uses a local mock identity/API service and
test-only credentials. It does not establish live identity-provider
connectivity or a deployed production authentication integration.

The following remain outside this acceptance:

- A deployment-specific production transport adapter.
- Production secret-manager integration and real key provisioning/rotation.
- A live authenticated production Next.js-to-FastAPI request.
- Deployed network, TLS and workload identity boundary verification.
- Independent production deployment security review.

Production security certification is NOT CLAIMED.
Production OIDC enablement remains NOT APPROVED.
