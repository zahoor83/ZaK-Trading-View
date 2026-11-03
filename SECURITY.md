# Security Policy

## Product Security Boundary

ZaK Trading View is a market analysis, signal generation and virtual paper-trading platform.

Real exchange order placement, modification and cancellation are prohibited.

Real exchange trading credential flows are prohibited.

Only public market data is permitted.

## Secret Management

- Never commit `.env` files.
- Never commit passwords, tokens or private keys.
- Use `.env.example` for empty configuration placeholders.
- Keep production secrets in an approved secret-management system.
- Rotate any credential that may have been exposed.

## Market Data Integrity

Validate:

- Data source and symbol identity.
- Event and receipt timestamps.
- Data freshness.
- Missing candles and sequence gaps.
- Duplicate and out-of-order events.
- Reconnection and backfill behavior.
- Provider errors and rate limits.
- Spot and Futures contract metadata.

Missing, stale, contradictory or untrusted inputs must fail closed.

## AI and Signal Safety

AI-generated analysis is not sufficient by itself to authorize an actionable signal.

Deterministic validation, risk checks and data-quality checks are mandatory.

Invalid evidence must produce WAIT or a blocked result.

## Paper Trading

Paper trading must use an internal virtual ledger.

It must never submit real exchange orders.

Spot and Futures portfolios must remain independent.

## Vulnerability Reporting

Do not publish secrets or exploitable vulnerability details publicly.

Report security issues privately to the project maintainer.

## Security Verification

Security checks must be executed and their actual results recorded in the relevant Part report.
