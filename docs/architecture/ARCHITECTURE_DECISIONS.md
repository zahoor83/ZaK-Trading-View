# ZaK Trading View — Architecture Decisions (Part 1)
Status: Approved direction; implementation is in progress under subsequent project parts. This document records approved architecture decisions, not current implementation completeness.

## ADR-001: Modular monolith
Next.js/TypeScript frontend, Python/FastAPI modular backend, separately executable background worker. Module boundaries are explicit; separate network microservices are not required initially.

## ADR-002: Mobile strategy
Responsive PWA first. Native React Native application deferred until web contracts and user flows stabilize.

## ADR-003: Persistence
PostgreSQL 18 as authoritative persistence; SQLAlchemy 2 and Alembic. Redis is optional/deferred and must not become the sole source of portfolio or signal state. PostgreSQL server currently stopped; no database connectivity verified.

## ADR-004: Public market data only
Binance public Spot/Futures REST and WebSocket; validate exchange/symbol metadata, event timestamps, freshness, ordering, duplicates, gaps, reconnects, rate limits and provider errors. Fail closed on uncertain data. Do not invent prices.

## ADR-005: AI decision separation
Specialist agents propose evidence-backed analyses. Deterministic validation and risk checks gate actionable LONG/SHORT; uncertain inputs yield WAIT/BLOCKED. Track model/prompt/version, provenance, freshness, expiry and rationale.

## ADR-006: No real trading
No authenticated exchange trading credentials, no real order endpoints, no order creation/amend/cancel. Automated execution only against internal simulated paper ledger. Defense in depth includes outbound network restrictions and tests.

## ADR-007: Paper accounting
Independent Spot and Futures virtual portfolios, each initially 1000 USDT. Persist balances and ledger transactions; no reset on refresh. Reset only selected portfolio with confirmation and retain historical sessions. Use atomic DB transactions and idempotency keys.

## ADR-008: Charting and indicators
Use a legally compatible charting library; verify license before adoption. Indicators require documented formulas, warm-up, missing-data behavior and tests. Do not copy proprietary TradingView assets.

## ADR-009: Testing and release
Pytest unit/integration/security; Playwright browser E2E; contract, regression, failure-injection and simulated accounting tests. No PASS without executed test evidence. Linux production deployment deferred.

## ADR-010: Secrets and operations
No secrets in Git. Environment templates only. Least privilege, audit logs, rate limits, authentication and authorization before public beta. Git initialization/remote/deployment require explicit approval in their respective parts.

## Known environment
Root: D:\ZaK Trading View; Windows 10 Home build 19045; Python 3.13.15; Node 24.19.0; npm 11.17.0; Git 2.55.0.windows.3; VS Code 1.144.0; PostgreSQL psql 18.6, server stopped and localhost:5432 no response; WSL 2.7.14 Ubuntu stopped; Docker and Redis commands not detected.
