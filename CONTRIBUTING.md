# Contributing to ZaK Trading View

## Development Rules

1. Follow the approved architecture.
2. Work on one development Part at a time.
3. Do not begin the next Part before approval.
4. Do not assume an unverified implementation exists.
5. Inspect existing files before modification.
6. Preserve existing functionality and uncommitted work.
7. Provide complete file contents for new files.
8. Do not install dependencies without approval.
9. Record actual test commands and outputs.
10. Never claim a test passed without evidence.

## Security Rules

- Never commit credentials or secrets.
- Never introduce real exchange order execution.
- Never add exchange trading credential flows.
- Use Binance public market data only.
- Fail closed on invalid or stale market data.
- Keep virtual Spot and Futures portfolios separate.

## Git Rules

- Review `git status` before changes.
- Review the complete staged diff.
- Do not force push.
- Do not destroy uncommitted work.
- Do not connect a remote without explicit approval.

## Documentation

Maintain exactly one consolidated Markdown report per development Part under `docs/reports/`.

The report must contain implementation details, tests, security audit, acceptance results and continuation snapshot.
