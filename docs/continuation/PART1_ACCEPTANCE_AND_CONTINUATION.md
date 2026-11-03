# ZaK Trading View — Acceptance and Continuation Snapshot

Snapshot updated: 2026-11-03

## 1. Project Identity

- Project: ZaK Trading View
- Confirmed absolute root: `D:\ZaK Trading View`
- Operating system: Windows 10 Home, build 19045
- Development environment: VS Code and PowerShell
- Project approach: brand-new zero-start project
- Legacy project code, configuration, Git history and database were not reused.

## 2. Approved Architecture

The user approved the proposed architecture and PWA-first mobile approach.

| Layer | Approved direction |
|---|---|
| Architecture | Modular Monolith |
| Frontend | Next.js, React, TypeScript |
| Mobile | Responsive PWA first |
| Backend | Python 3.13, FastAPI |
| Database | PostgreSQL 18 |
| ORM | SQLAlchemy 2 |
| Migrations | Alembic |
| Cache | Redis, deferred |
| Market data | Binance public Spot/Futures APIs |
| AI | Specialist agents with deterministic validation |
| Paper trading | Internal virtual ledger only |
| Testing | Pytest and Playwright |
| Deployment | Linux production target, deferred |

Architecture approval does not mean that every application layer or dependency has been implemented.

## 3. Part 1 — Architecture Decisions and Zero-Start Blueprint

**Status: COMPLETE — planning and acceptance scope only.**

Part 1 established the approved architecture, MVP scope, safety contract and unresolved decisions.

The original acceptance evidence recorded:
- Confirmed project root and target OS.
- Reported development-tool version checks.
- Explicit user approval of architecture and PWA-first approach.
- Four planning documents reported copied and SHA-256 verified.
- Initial folder-creation and required-path checks reported by the user.

Part 1 did not include application implementation, database connectivity, package installation, Git initialization, automated application tests or deployment.

Original Part 1 environment snapshot recorded PostgreSQL service stopped and localhost port 5432 unavailable. This was a historical Part 1 observation; PostgreSQL was subsequently recovered and verified during Part 2. The later recovery does not retroactively expand Part 1's planning-only acceptance.

### Part 1 authoritative planning documents

- `docs\architecture\ARCHITECTURE_DECISIONS.md`
- `docs\architecture\MVP_SCOPE.md`
- `docs\decisions\OPEN_DECISIONS.md`
- `docs\continuation\PART1_ACCEPTANCE_AND_CONTINUATION.md`

### Part 1 unresolved decisions

The following remain open for their relevant future Parts and are not silently approved:
- Binance market coverage, geographic/API availability and fallback behavior.
- Freshness thresholds, reconnection and historical backfill policies.
- Chart library selection and licensing.
- AI provider/model, privacy, retention and cost controls.
- Indicator formulas and actionable-signal thresholds.
- Futures paper-trading margin, leverage, funding, fees and liquidation rules.
- Spot SHORT signal semantics.
- Subscription quota and timezone rules.
- Trial eligibility and abuse controls.
- Authentication and notification providers.
- Database credentials, Redis and background queue implementation.
- Exact dependency versions and deployment configuration.

## 4. Part 2 — New Repository and Development Environment

**Status: COMPLETE — documented Part 2 acceptance scope.**

Part 2 acceptance report: `docs\reports\PART2_REPOSITORY_AND_ENVIRONMENT.md`.

Previously verified environment evidence included:
- Python 3.13.15.
- Node.js 24.19.0.
- npm 11.17.0.
- Git 2.55.0.windows.3.
- VS Code 1.144.0.
- PostgreSQL client 18.6.
- PostgreSQL 18 Windows service `postgresql-x64-18` recovered to Running with Automatic startup.
- PostgreSQL accepted local connections and authenticated SQL execution was successful at the recorded verification.

Documented Part 2 limitations remain:
- A complete backup/restore or disaster-recovery drill was not verified.
- Windows reboot and automatic-startup behavior was not independently tested as a full recovery drill.
- Application-specific database roles/permissions and Alembic migrations were not verified in that Part.
- Comprehensive secret scanning and production-grade disaster-recovery/security testing were not completed.

## 5. Part 3 — Signal-Only Safety Boundary

**Status: ACCEPTED WITH DOCUMENTED SCOPE LIMITATIONS.**

Part 3 implementation commit:
- `0be8aee57f35054c1e9d4e254c7c344885ae87eb`
- Message: `feat: establish signal-only security and egress controls`

The Part 3 commit is present in the repository history. The current `main` remote tip is a later commit that includes the Part 3 commit in its history. The historical statement that Part 3 had no commit or push is superseded and must not be reused.

### Part 3 implementation files

- `apps\api\app\security\__init__.py`
- `apps\api\app\security\execution_boundary.py`
- `apps\api\app\security\egress_policy.py`
- `apps\api\app\security\public_http.py`
- `apps\api\app\security\websocket_egress.py`
- `apps\api\app\security\websocket_messages.py`
- `apps\api\app\security\public_websocket.py`

### Part 3 tests and dependencies

- `apps\api\tests\test_execution_boundary.py`
- `apps\api\tests\test_egress_policy.py`
- `apps\api\tests\test_egress_policy_headers.py`
- `apps\api\tests\test_public_http.py`
- `apps\api\tests\test_public_http_configuration.py`
- `apps\api\tests\test_public_websocket.py`
- `apps\api\tests\test_websocket_egress.py`
- `apps\api\tests\test_websocket_messages.py`
- `apps\api\requirements.txt`
- `apps\api\requirements-dev.txt`

### Part 3 reports

- `docs\reports\PART3_SIGNAL_ONLY_SAFETY_BOUNDARY.md`
- `docs\reports\PART3_REAL_ORDER_ISOLATION_READONLY_AUDIT.txt`
- `docs\reports\PART3_WINDOWS_EGRESS_BASELINE.txt`
- `docs\reports\PART3_FINAL_ACCEPTANCE.md`
- `docs\reports\PART1_RECONCILIATION_READONLY_AUDIT.txt`

### Part 3 fixed five-phase acceptance workflow

1. Real-order execution isolation audit — **COMPLETE, static source-audit scope only.**
2. Windows network egress security — **COMPLETE, application-level policy and transport scope only; OS/process-level outbound isolation deferred.**
3. Part 1 acceptance reconciliation — **COMPLETE, documentation reconciled in this snapshot and the Part 3 final report.**
4. Full regression and security verification — **COMPLETE within the tested scope described below.**
5. Final Part 3 acceptance documentation — **COMPLETE after this report and continuation snapshot are verified and committed.**

### Part 3 fresh verification evidence — 2026-11-03

The user supplied fresh local PowerShell output:

| Verification | Observed result |
|---|---|
| Python version | 3.13.15 |
| Full pytest suite | 67 passed |
| Pytest subtests | 117 passed |
| `python -m compileall -q app tests` | Exit code 0 |
| `python -m pip check` | No broken requirements found; exit code 0 |
| `git diff --check` before documentation update | Exit code 0 |
| Additional HTTP adversarial URL probes | 6 of 6 rejected |
| Additional WebSocket adversarial URL probes | 5 of 5 rejected |

The 11 additional adversarial cases tested trailing-dot hosts, empty user information, malformed port syntax, encoded credential query names, duplicate signature parameters, backslash authority ambiguity and duplicate WebSocket stream parameters. All 11 were rejected; no unexpected exception was reported.

### Part 3 source and test audit findings

- No known order mutation terms were found in the audited application source by the supplied scan.
- No exchange client import or constructor was found by the supplied scan.
- `websockets.asyncio.client.connect` is the actual public WebSocket transport import.
- The `api_key` scan match is a prohibited query-parameter name in the HTTP egress policy, not evidence of an actual credential value or credential flow.
- The HTTP transport uses `urllib.request`; the WebSocket transport uses the `websockets` client.
- Existing tests verify validation before HTTP transport creation and before WebSocket connection attempts.
- Existing tests cover allowed/denied HTTP hosts and paths, methods, headers, query parameters, malformed URLs, redirect rejection, proxy configuration, TLS configuration, timeouts, response-size limits, WebSocket hosts/paths/streams, outbound subscription messages and execution-mode helper behavior.

### Part 3 accepted limitations

- The execution-mode helper is not evidence of complete application request-flow integration. `apps\api\app\main.py` remains a placeholder in the inspected scope.
- Static pattern scanning does not prove runtime isolation against all dynamic or indirect execution paths.
- Incoming WebSocket messages are delegated as raw messages; full incoming event-schema validation belongs to later market-data ingestion work.
- Live market-data ingestion, routing and virtual paper-ledger functionality belong to their respective future Parts.
- Windows Firewall profiles were enabled, but current inspection showed `DefaultOutboundAction = Allow` for Domain, Private and Public profiles, with no enabled outbound block rules found. No Python-specific outbound restriction was established.
- Windows process/domain-level outbound enforcement, production network isolation and production penetration testing are not included in Part 3 acceptance.
- Future frontend/configuration security, application integration and end-to-end testing remain future work.

### Part 3 acceptance meaning

Part 3 is accepted only within its documented application-level signal-only safety and public market-data egress policy/transport scope. This is not production certification, complete operating-system network isolation or proof that future application request flows are already integrated.

## 6. Current Git and Repository State

Fresh user-provided verification on 2026-11-03, before this documentation update:

- Branch: `main`.
- Local HEAD: `a3fc738a08476ad99d30018c2285e4d6fac24e4d`.
- Local `origin/main`: `a3fc738a08476ad99d30018c2285e4d6fac24e4d`.
- `git ls-remote origin refs/heads/main`: `a3fc738a08476ad99d30018c2285e4d6fac24e4d`.
- Working tree was clean.
- Latest commit message: `docs: finalize Part 2 repository and environment acceptance`.
- Part 3 implementation commit `0be8aee57f35054c1e9d4e254c7c344885ae87eb` is in the previously verified history.

The documentation update described by this snapshot is a new local change and must not be represented as committed or pushed until the subsequent Git commands verify that result.

## 7. Part 3 Final Acceptance Verdict

**PART 3: ACCEPTED WITH DOCUMENTED SCOPE LIMITATIONS.**

Acceptance evidence consists of the source/test audit, fresh regression and compile/dependency checks, 11 additional rejected adversarial URL probes, the Windows Firewall baseline inspection and the explicit limitations recorded above.

No real exchange order placement, modification, cancellation, authenticated trading credential flow or live trading integration is authorized or introduced by Part 3.

## 8. Future Parts and Continuation Rules

- Part 1 — COMPLETE, planning and acceptance scope only.
- Part 2 — COMPLETE, documented repository/environment scope.
- Part 3 — ACCEPTED WITH DOCUMENTED SCOPE LIMITATIONS.
- Part 4 onward — PENDING. Do not begin any later Part without explicit user approval.

Preserve the established workflow:
- No guessing; inspect before modifying.
- Preserve existing files and uncommitted work.
- Do not install dependencies or change firewall policy without explicit approval.
- Do not claim a test passed without actual output.
- Do not use destructive Git operations or force push without explicit authorization.
- Complete, test, audit and document one Part before requesting approval for the next.
- After an approved Part is complete, commit and push only reviewed, intended files; verify local/remote commit hashes and working-tree status.

**Next action:** Complete final documentation diff review, rerun the approved verification suite, then commit and push the two reviewed Part 3 documentation files. Stop after Part 3 acceptance; do not begin Part 4 without explicit user approval.
