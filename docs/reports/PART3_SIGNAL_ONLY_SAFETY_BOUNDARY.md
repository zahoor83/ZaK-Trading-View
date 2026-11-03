# Part 3 — Signal-Only Safety Boundary

**Project:** ZaK Trading View
**Part:** 3 — Signal-Only Safety Boundary
**Date:** 2026-11-03
**Branch:** `main`
**Initial repository commit:** `b3e49d4`
**Part 3 commit:** Not created
**Part 3 push:** Not performed

## 1. Objective

Establish a fail-closed signal-only execution boundary and application-level network egress controls.

ZaK Trading View is an analysis and virtual paper-trading platform. Real exchange trading is prohibited.

Part 3 establishes the security foundations and verification evidence. It does not implement the later market-data ingestion, signal-generation or paper-ledger product functionality.

## 2. Previous Parts

- Part 1 — Architecture Decisions & Zero-Start Blueprint: COMPLETE (planning and acceptance scope).
- Part 2 — New Repository & Development Environment: COMPLETE.
- Initial GitHub commit: `b3e49d4`.
- GitHub repository: `zahoor83/ZaK-Trading-View` (private).
- Current branch: `main`, tracking `origin/main`.

Historical Part 1 and Part 2 audit reports remain unchanged. Their historical snapshots must not be interpreted as the current repository state.

## 3. Threat Model

- A caller supplies an unsupported `real`, `live` or `exchange` execution mode.
- Future application code introduces known Binance order endpoints or order-submission methods.
- Future application code introduces known Binance trading credential names.
- A signal is incorrectly treated as authorization to place an exchange order.
- An HTTP request attempts to reach an unapproved host, path, method or transport configuration.
- A WebSocket connection attempts to reach an unapproved endpoint or use an unsupported subscription message.
- Redirects, inherited proxies, malformed URLs, oversized responses or ambiguous input bypass intended transport restrictions.

## 4. Permitted and Prohibited Capabilities

Permitted by product policy:

- Public market-data access through explicitly approved endpoints.
- Evidence-backed market analysis.
- Internal virtual paper trading in its separately implemented project Part.

Prohibited:

- Real exchange order submission, amendment, cancellation or replacement.
- Exchange withdrawals, transfers or deposits.
- Authenticated exchange trading credentials.
- Any real, live or exchange execution mode.
- Private authenticated Binance WebSocket channels or trading endpoints.

These prohibitions remain applicable to all future project Parts.

## 5. Implemented Security Boundary

### 5.1 Execution-mode policy

`apps/api/app/security/execution_boundary.py` defines an explicit allowlist containing only `paper`.

Unsupported strings and non-string values are rejected with `UnsupportedExecutionMode`. String input is normalized for whitespace and letter case.

This helper establishes policy validation. It is not evidence of a complete application request-flow integration or a paper ledger.

### 5.2 REST / HTTP egress policy

`apps/api/app/security/egress_policy.py` validates outbound public REST requests before transport.

The policy restricts requests to:

- HTTPS.
- Exact approved Binance Spot and USD-M Futures hosts.
- Explicitly approved public GET paths.
- Standard HTTPS port.
- Approved request headers.
- Rejected credential-related query keys.
- Rejected URL user information, fragments, ambiguous paths and control/whitespace characters.

### 5.3 HTTP transport

`apps/api/app/security/public_http.py` provides the restricted HTTP transport.

Implemented controls include:

- Policy validation before sending.
- Redirect rejection.
- No inherited environment proxy configuration.
- Default SSL certificate verification.
- Finite positive request timeout.
- Maximum response-size enforcement.
- GET-only transport.

### 5.4 WebSocket URL and message policy

The WebSocket security modules validate public Spot and USD-M Futures WebSocket URLs and subscription messages.

Controls include:

- WSS-only transport.
- Exact approved public hosts and paths.
- No user information or fragments.
- Rejection of control and whitespace characters in URLs.
- Approved subscription methods only.
- Strict message parsing, including duplicate-key rejection.
- Subscription parameter and message-size limits.
- Rejection of unsupported message structures.

### 5.5 WebSocket transport

`apps/api/app/security/public_websocket.py` uses the approved WebSocket URL policy before connection.

The transport uses a default SSL context, explicitly disables proxy inheritance and compression, and sets finite connection, close and heartbeat limits.

Incoming server messages are not yet validated against a complete market-data event schema. That responsibility belongs to the later ingestion implementation.

## 6. Real-Order Isolation Audit

The read-only source audit is recorded in:

`docs/reports/PART3_REAL_ORDER_ISOLATION_READONLY_AUDIT.txt`

The reported static scan covered four application/security-related source files and found no matches for the audited known Binance real-order endpoints, order-submission or mutation methods, trading credential identifiers, or exchange client constructors/imports.

The test file was excluded from application-source findings.

**Limitation:** Static pattern scanning is not proof against dynamically constructed URLs, indirect calls, future dependencies, arbitrary network clients or runtime behavior. The audit is limited to the inspected source scope.

## 7. Automated Verification

The final reported verification results are:

- Pytest: **67 passed**.
- Pytest subtests: **117 passed**.
- `python -m compileall -q app tests`: passed.
- `python -m pip check`: no broken requirements found.
- `git diff --check`: no whitespace errors; LF/CRLF conversion warnings only.
- REST URL control-character probes: rejected.
- WebSocket URL control-character probes: rejected.
- Audited real-order pattern scan: no listed application-source matches.

These results are based on user-provided local command output. They do not certify untested runtime, production or deployment behavior.

## 8. Windows Network Egress Decision

The Windows baseline is recorded in:

`docs/reports/PART3_WINDOWS_EGRESS_BASELINE.txt`

The inspected Windows Firewall profiles were enabled, but their default outbound action was Allow. No matching enabled outbound Block rule or Python-specific restriction was identified in the reported inspection.

The project did not change global Windows Firewall policy or block the shared Python executable.

**Accepted scope:** Application-level exact-host/path/method egress policy and restricted transports.

**Deferred scope:** Complete Windows operating-system or process-level outbound network isolation. This remains a documented limitation and must be reconsidered when the executable identity, deployment model and operational impact are known.

## 9. Limitations and Deferred Controls

- The execution-mode helper is not evidence of complete request-flow integration.
- Static source scanning is not a runtime security guarantee.
- WebSocket incoming event-schema validation belongs to later ingestion work.
- Public market-data ingestion and business workflows are not accepted as complete by this Part.
- Paper-ledger isolation belongs to its later implementation Part.
- Authentication, authorization, user management and production secrets management belong to their respective later Parts.
- Windows process-level outbound isolation is deferred.
- Linux production deployment and production certification are not part of this acceptance.
- No real exchange adapter or order-execution interface is permitted.

## 10. Git and Acceptance

The final acceptance report is:

`docs/reports/PART3_FINAL_ACCEPTANCE.md`

Its verified SHA-256 is:

`264C0D387DB0CF44509B6CE2EE685FEFFC290242E168462B955334BB58FA44A0`

At the time of the reported verification:

- Branch: `main`, tracking `origin/main`.
- Part 3 files remain local and uncommitted.
- No Part 3 commit has been created.
- No Part 3 push has been performed.

**Acceptance verdict: PART 3 — ACCEPTED WITH DOCUMENTED SCOPE LIMITATIONS.**

This verdict applies only to the implementation and verification scope described in the final acceptance report. It is not production certification or a claim of complete operating-system network isolation.

Part 4 must not begin without explicit user approval.
