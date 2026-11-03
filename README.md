# ZaK Trading View

AI-powered cryptocurrency market analysis and signal platform.

## Project Status

Architecture planning is complete.

Repository and development environment setup is in progress.

Application features have not yet been implemented.

## Product Scope

- Binance public Spot and Futures market data.
- Interactive market charts and technical indicators.
- AI-assisted market analysis.
- Futures and Spot signal generation.
- LONG, SHORT and WAIT decisions.
- Virtual paper trading.
- Watchlists, alerts and market scanning.
- Historical backtesting and analytics.
- Responsive web application and PWA.

## Mandatory Trading Safety

This platform is strictly a signal and simulation system.

- No real exchange order placement.
- No real exchange order modification or cancellation.
- No real exchange trading credential flows.
- Public market data only.
- Paper trading uses an internal virtual ledger.
- Spot and Futures portfolios remain independent.
- Stale, missing or contradictory data must block actionable signals.

## Architecture

- Frontend: Next.js, React and TypeScript.
- Backend: Python and FastAPI.
- Database: PostgreSQL.
- ORM: SQLAlchemy.
- Migrations: Alembic.
- Cache: Redis, deferred.
- Background processing: separate worker process.
- Testing: Pytest and Playwright.
- Mobile: responsive PWA first.

See `docs/architecture/` for approved architecture and MVP documentation.

## Development Environment

Verified development environment:

- Windows 10 Home, build 19045.
- Python 3.13.15.
- Node.js 24.19.0.
- npm 11.17.0.
- Git 2.55.0.windows.3.
- VS Code 1.144.0.
- PostgreSQL client 18.6.

PostgreSQL server was installed but stopped during initial inspection.

Docker and Redis CLI were not detected.

## Initial Setup

Open PowerShell in the project root:

```powershell
Set-Location -LiteralPath "D:\ZaK Trading View"
```

Verify installed tools:

```powershell
python --version
node --version
npm --version
git --version
```

Create a Python virtual environment when backend dependency setup is approved:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Copy the example environment file for local development when configuration is required:

```powershell
Copy-Item .env.example .env
```

Never commit `.env`.

Application dependencies and installation commands will be documented in their respective implementation parts. Do not install packages based on this initial repository document alone.

## Database

PostgreSQL 18 is the approved database direction.

Before database implementation:

1. Verify PostgreSQL server service.
2. Confirm local connectivity.
3. Confirm database user and permissions.
4. Keep credentials outside version control.
5. Apply migrations through Alembic.

Redis installation and configuration are deferred until explicitly approved.

## Testing

Planned testing layers:

- Unit tests.
- API tests.
- Integration tests.
- Security tests.
- Browser end-to-end tests.
- Market-data validation tests.
- Paper-trading accounting tests.
- Regression tests.

Never report tests as passed without actual execution output.

## Git Workflow

- Local Git repository initialized on branch `main`.
- GitHub repository: https://github.com/zahoor83/ZaK-Trading-View
- GitHub visibility: Private (independently verified through authenticated GitHub API).
- Local Git remote is not yet configured.
- No initial commit or push has been performed.

Before committing:

```powershell
git status --short
git diff --check
```

Review staged files before every commit.

## Recovery

Before destructive Git operations:

1. Inspect `git status`.
2. Inspect current branch and commit.
3. Preserve uncommitted work.
4. Create a verified backup outside the repository.
5. Never use `git reset --hard`, `git clean -fd`, or force push without explicit authorization.

Local Git history is not a substitute for independent backups.

## Documentation

Each development Part has one consolidated Markdown report under `docs/reports/`.

Every Part must include its implementation record, actual test evidence, security audit, acceptance criteria and continuation snapshot.

The next Part cannot begin until the current Part is verified and approved.

## Security

See `SECURITY.md` and `CONTRIBUTING.md`.
