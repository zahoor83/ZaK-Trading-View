# ZaK Trading View — Paper Trading Architecture Contract

**Phase:** 2 — Architecture & Data Contract  
**Status:** FINAL - Phase 2 Architecture and Data Contract (design-level acceptance; implementation deferred)
**Scope:** Paper Trading only  
**Financial currency:** USDT  
**Starting capital:** 1,000 USDT per portfolio session

---

## 1. Purpose

Define the financial architecture, accounting invariants, session lifecycle,
authorization policy, and database requirements for Paper Trading.

This document does not authorize live trading, real exchange orders, or
unverified migration of existing financial records.

## 2. Approved architectural decisions

1. PostgreSQL is authoritative for financial state.
2. An immutable transaction ledger is the financial source of truth.
3. A balance snapshot is maintained transactionally for fast reads.
4. Ledger and balance snapshot changes must commit atomically.
5. A ledger/snapshot reconciliation mismatch must fail closed.
6. Historical ledger entries must not be silently updated or deleted.
7. Corrections must use compensating ledger entries.
8. Spot and Futures must have independent portfolios, sessions, ledger books,
   chart-of-account categories, and cash snapshots.
9. Browser refresh must never reset or replenish financial balances.
10. No real funds or real exchange orders are permitted.

## 3. Portfolio and session isolation

Each Trading Account has:

- One Spot portfolio.
- One Futures portfolio.
- Independent session histories for both portfolios.

Each active portfolio session has exactly one ledger book containing its
own six-category chart of accounts.

Initial session capital:

- Spot: 1,000 USDT.
- Futures: 1,000 USDT.

No transfer between Spot and Futures is permitted.

A ledger book belongs to exactly one Trading Account, Paper Portfolio,
and Paper Session. Each chart-of-account category belongs to exactly one
ledger book. A session must belong to the same portfolio and account as
its ledger book.

Archived sessions and their financial history must remain available.

## 4. Financial source of truth

The immutable double-entry journal is authoritative.

A ledger book belongs to exactly one Trading Account, Paper Portfolio,
and Paper Session. Each book contains six chart-of-account categories.
The cash balance snapshot is a derived projection of the
`cash_available` account and is never an independent source of truth.

Every financial mutation must:

1. Authenticate and authorize the request against trusted server-side identity.
2. Lock the relevant Trading Account, portfolio, session, ledger book,
   and idempotency scope in a deterministic order.
3. Validate idempotency and the canonical request fingerprint.
4. Reconcile journal postings, chart-of-account balances, positions,
   and cash snapshot before applying a new operation.
5. Validate business rules, exact decimal values, market filters,
   position state, fees, and available cash/margin.
6. Create one journal transaction header and its complete debit/credit
   postings.
7. Verify that total debits exactly equal total credits for the
   transaction and currency.
8. Update affected position state and all derived balance snapshots
   in the same database transaction.
9. Reconcile the resulting journal, positions, and snapshots before commit.

Any failure must roll back the entire financial mutation.

A posted journal transaction and its entries must never be silently
updated or deleted. Corrections require a new compensating transaction.

A snapshot must never be silently repaired from an unverified value.
A mismatch must fail closed and be resolved through an auditable
accounting correction.

## 5. Proposed financial entities

These are proposed logical entities. Exact SQLAlchemy fields, constraints,
indexes, and migration details remain subject to source reconciliation.

### 5.1 paper_ledger_books

One ledger book per portfolio session. This is the financial container
for that exact Trading Account, Paper Portfolio, and Paper Session.

Proposed attributes:

- UUID primary key.
- Trading Account UUID.
- Paper Portfolio UUID.
- Paper Session UUID.
- Currency (USDT).
- Immutable original starting capital.
- Derived available-cash snapshot.
- Accounting version and fee-configuration version.
- Book status and creation/update timestamps.

Required invariants:

- Exactly one ledger book per portfolio session.
- The book must reference the exact account/portfolio/session ownership tuple.
- Original starting capital for a newly initialized session is 1,000 USDT.
- Manual Top-up must never rewrite original starting capital.
- Available-cash snapshot is derived from the `cash_available` account.
- Spot and Futures must have separate books, sessions, and snapshots.

### 5.2 paper_ledger_accounts

The chart of accounts contains exactly six accounting categories
for each ledger book. These are accounting categories inside a book,
not six independent portfolios or user-facing wallets.

Proposed attributes:

- UUID primary key.
- Ledger Book UUID.
- Account code.
- Currency (USDT).
- Derived account-balance snapshot, if retained.
- Normal-balance convention.
- Creation timestamp.

Required account codes:

1. `cash_available` — available virtual cash; debit-normal asset.
2. `spot_position_cost` — Spot position cost basis; debit-normal asset.
3. `futures_margin_locked` — locked Futures margin; debit-normal asset.
4. `realized_pnl` — realized trading gains/losses; credit balance represents
   gains and debit balance represents losses.
5. `fee_expense` — trading fees; debit-normal expense.
6. `contributed_capital` — initial and manually contributed virtual capital;
   credit-normal equity.

Required invariants:

- Unique `(ledger_book_id, account_code)` pair.
- Exactly one row for each of the six required account codes per ledger book.
- All six category rows must be created atomically with ledger-book
  provisioning; a unique constraint alone does not prove category completeness.
- A deferred database constraint trigger or an equivalently restricted,
  trusted provisioning mechanism must verify that every committed book
  has all six required categories and no extras.
- `spot_position_cost` postings are permitted only in Spot books.
- `futures_margin_locked` postings are permitted only in Futures books.
  These market-type restrictions must be enforced by the database and
  revalidated by the trusted accounting service.
- No account category can be shared across ledger books.
- Account balances are derived from posted journal entries.
- Account snapshots must reconcile to journal postings.
- Account code and book ownership become immutable after provisioning.

### 5.3 paper_ledger_transactions

One immutable journal transaction header per business financial operation.

Proposed attributes:

- UUID primary key.
- Ledger Book UUID.
- Operation type.
- Posting status.
- Idempotency/request reference.
- Business reference (position, top-up, reset, correction, or funding).
- Canonical request fingerprint reference.
- Accounting version.
- Creation and posting timestamps.
- Non-secret audit metadata.

Required invariants:

- A posted transaction belongs to exactly one ledger book.
- A posted transaction contains at least two journal entries.
- All entries in a transaction use the book currency.
- A transaction cannot be posted unless its debit and credit totals balance exactly.
- Posting status transitions must not permit a posted transaction to be
  edited back into a draft or silently rewritten.
- A failed operation must not leave a partially posted transaction.
- Corrections use a new compensating transaction linked to the original.

### 5.4 paper_ledger_entries

Immutable debit/credit journal postings belonging to one transaction.

Proposed attributes:

- UUID primary key.
- Journal Transaction UUID.
- Ledger Book UUID.
- Ledger Account UUID (one of the six categories).
- Entry side: `DEBIT` or `CREDIT`.
- Positive exact monetary amount.
- Currency (USDT).
- Stable line sequence within the transaction.
- Creation timestamp.

Required invariants:

- Amount must be strictly positive; zero and negative posting amounts are invalid.
- Each entry's ledger account must belong to the same ledger book as its transaction.
- Each posted transaction must contain at least one debit and one credit.
- Sum of debit amounts must equal sum of credit amounts exactly.
- No binary floating-point arithmetic is authoritative.
- Posted entries cannot be updated or deleted by ordinary application roles.
- Corrections must be represented by new compensating postings.
- Database enforcement must use a verified deferred constraint/trigger
  or a restricted trusted posting mechanism; a simple row CHECK alone
  cannot prove a cross-row transaction balance.

Required posting examples (USDT):

Initial funding of 1,000:

| Account | Debit | Credit |
|---|---:|---:|
| `cash_available` | 1,000.00 | — |
| `contributed_capital` | — | 1,000.00 |

Spot purchase with notional 100 and entry fee 0.10:

| Account | Debit | Credit |
|---|---:|---:|
| `spot_position_cost` | 100.00 | — |
| `fee_expense` | 0.10 | — |
| `cash_available` | — | 100.10 |

Spot sale postings must remove the original cost basis, record realized
gain or loss, recognize the exit fee, and credit/debit cash for net
proceeds. Gain and loss direction must be determined from exact
proceeds versus the recorded cost basis; the journal must balance.

Futures margin lock:

| Account | Debit | Credit |
|---|---:|---:|
| `futures_margin_locked` | 100.00 | — |
| `cash_available` | — | 100.00 |

Manual Top-up of 925:

| Account | Debit | Credit |
|---|---:|---:|
| `cash_available` | 925.00 | — |
| `contributed_capital` | — | 925.00 |

The examples describe accounting direction, not authorization to
execute any real exchange order.

### 5.5 paper_positions

Persistent open and closed paper positions.

Proposed attributes:

- UUID primary key.
- Trading Account UUID.
- Paper Portfolio UUID.
- Paper Session UUID.
- Ledger Book UUID.
- Symbol and market.
- Side and margin mode.
- Exact entry price and quantity.
- Leverage and margin.
- Entry fee amount and entry fee-rate snapshot.
- Exit price, exit fee amount, and exit fee-rate snapshot.
- Gross and net realized P&L.
- Accounting version and market-metadata version/reference.
- Position status and lifecycle timestamps.

Required invariants:

- Position ownership must match its ledger book, account, portfolio,
  and session through database-enforced composite relationships.
- Spot and Futures positions must never share a ledger book.
- Price and quantity retain exact decimal representations.
- Entry and exit fee configuration must be retained for historical reproducibility.
- Position changes and corresponding journal postings commit atomically.
- Open positions and locked margin must not be deleted or silently released.
- Unrealized P&L/equity must not be fabricated without verified market prices.

### 5.6 paper_execution_requests

Idempotency and operation tracking across financial mutations.

Proposed attributes:

- UUID primary key.
- Trading Account UUID.
- Paper Portfolio UUID.
- Ledger Book UUID where an operation is session-scoped.
- Operation type.
- Idempotency key.
- Canonical request fingerprint.
- Processing status.
- Resulting journal transaction, session, or position reference.
- Creation and completion timestamps.
- Non-secret audit metadata.

Required invariants:

- Trade operations are scoped to the owning ledger book and operation type.
- Reset idempotency must remain scoped to the Trading Account,
  portfolio, and reset operation across the session transition; it must
  not become a fresh operation merely because a new session was created.
- The same scope and key with an identical fingerprint returns the
  original result without another financial effect.
- The same scope and key with a different fingerprint is rejected.
- Concurrent duplicate requests serialize safely.
- Failed transactions must not leave partial financial mutations.
- Secrets and credentials must never be stored in request fingerprints
  or non-secret audit metadata.

### 5.7 Schema reconciliation and decimal precision contract

The existing V2 application schema already provides:

- UUID-based Trading Accounts, Paper Portfolios, and Paper Sessions.
- One Spot and one Futures portfolio per Trading Account.
- Composite account/portfolio ownership constraints for sessions.
- One active session per portfolio, enforced by a partial unique index.
- A unique session number within each portfolio.

The existing schema does not yet provide ledger books, chart-of-account
rows, journal transactions and entries, persistent paper positions, or
paper execution request records. These remain future financial-schema
additions.

Financial tables must preserve account, portfolio, and session ownership
using database-enforced foreign keys. A financial record must not be
able to reference a session belonging to another portfolio or account.

Financial ownership constraints:

- `paper_ledger_books` must retain the Trading Account UUID, Paper
  Portfolio UUID, and Paper Session UUID as one ownership tuple.
- A ledger book must reference a portfolio belonging to the same
  Trading Account through a database-enforced composite foreign key.
- A ledger book must reference a session belonging to that exact
  account and portfolio through a composite foreign key.
- Required ownership combinations must have corresponding unique
  database keys.
- `paper_ledger_accounts` must reference its ledger book and enforce
  uniqueness of `(ledger_book_id, account_code)`. It must not be shared
  across books.
- `paper_ledger_transactions` must reference the owning ledger book.
- Each `paper_ledger_entries` row must reference a transaction, book,
  and chart-of-account row that all belong to the same ledger book.
  Composite foreign keys or an equivalently enforced database design
  must reject cross-book postings.
- `paper_positions` must reference the same Trading Account, portfolio,
  session, and ledger book through database-enforced ownership
  relationships.
- `paper_execution_requests` must enforce the correct account/portfolio
  scope and, for session-scoped operations, the correct ledger book.
- Cross-account, cross-portfolio, cross-session, and cross-book
  references must be rejected by database constraints.

Financial-history retention rules:

- Ledger entries must not be deleted or updated by ordinary application
  operations.
- Financial history must not be removed by cascading deletion of an
  account, portfolio, or session.
- Financial foreign keys must use restrictive deletion behaviour or an
  equivalent explicitly verified retention mechanism.
- Session reset is an archive-and-create-new-session operation, not
  deletion and recreation of historical financial records.
- Existing parent/session cascade relationships must be reviewed before
  financial foreign keys are introduced.

Decimal and precision rules:

1. Financial calculations must use exact decimal representations.
   Binary floating-point values must not be the authoritative accounting
   representation.
2. The current frontend decimal helper parses values into BigInt
   coefficients and decimal scales. Its supported input scale is 0–100
   and its maximum input text length is 128 characters.
3. Existing monetary calculations use a 2-decimal scaled-integer
   accounting path. The financial database contract must preserve the
   same USDT cash precision unless a separately versioned accounting
   decision explicitly changes it.
4. The decimal helper is generic; its existence does not prove that all
   financial values are restricted to two decimal places.
5. Price and quantity precision must be validated against verified
   market-symbol metadata, including tickSize and stepSize. Do not
   hard-code a universal price or quantity scale.
6. Price and quantity must retain exact decimal representations through
   API and persistence boundaries. Do not convert them to binary
   floating-point values for authoritative calculations.
7. Any rounding or rescaling must use an explicitly documented,
   deterministic rounding rule. Silent precision loss is prohibited.
8. Database NUMERIC precision/scale limits and maximum permitted
   monetary magnitude must be finalized from documented product limits
   before the financial migrations are authored. Do not invent a
   precision limit from the frontend helper's maximum input length.
9. API responses carrying authoritative financial decimals should use
   exact decimal strings or another lossless representation.

Financial idempotency and concurrency design:

- Trade idempotency uniqueness must be scoped to the owning ledger book
  and operation, with the exact scope enforced by a database constraint.
- Reset idempotency must be scoped to the Trading Account, Paper Portfolio,
  and reset operation across session transitions; it must not be scoped
  only to the old or newly created ledger book.
- A repeated key with an identical request fingerprint must return the
  original operation result without another financial effect.
- A repeated key with a different fingerprint must be rejected.
- Concurrent duplicate operations must serialize safely.
- Balance reconciliation, ledger insertion, snapshot update, and
  associated position mutation must occur in one database transaction.
- A failed operation must leave no partial financial mutation.

These requirements define the target contract. They do not claim that
the financial models, constraints, services, or runtime controls have
already been implemented or tested.

## 6. Manual Top-up policy

Manual Top-up is a separate, explicitly authorized financial operation.
It is not a reset and does not create a new session.

Trigger condition:

- Selected portfolio's reconciled `cash_available` balance is less than
  or equal to 100 USDT.

Required behaviour:

1. Display a top-up option only when the trigger condition is met.
2. Require explicit confirmation from an authorized Owner or Admin.
3. Lock and reconcile the selected ledger book before calculating the credit.
4. Calculate credit as 1,000 USDT minus current available cash.
5. Create one immutable `MANUAL_TOP_UP` journal transaction:
   debit `cash_available`; credit `contributed_capital`.
6. Require balanced postings and an idempotency key.
7. Update the cash snapshot atomically with the journal transaction.
8. Preserve original starting capital.
9. Preserve the current session, all positions, and locked Futures margin.
10. Browser refresh must never initiate top-up.
11. Reject stale, duplicate-with-different-payload, unauthorized, or
    unreconciled requests.

Example:

Current available cash: 75 USDT  
Top-up debit to contributed capital: 925 USDT  
Top-up credit to cash available: 925 USDT  
New available cash: 1,000 USDT

No automatic top-up is permitted.

## 7. Reset policy

Reset is a separate, explicitly authorized operation. It archives the
selected portfolio session and creates a new independent session/book.

Required behaviour:

1. Require explicit confirmation from an authorized Owner or Admin.
2. Lock and reconcile the selected portfolio and active ledger book.
3. Reject reset if any position remains open.
4. Reject reset if locked Futures margin or another unresolved
   position-related balance remains inconsistent with the closed-position state.
5. Never automatically close positions or silently release margin.
6. Archive the previous session without deleting its ledger book,
   chart of accounts, journal transactions, entries, positions, or history.
7. Create a new session and its independent ledger book.
8. Provision the six required chart-of-account categories for the new book.
9. Initialize the new session with a balanced 1,000 USDT funding transaction:
   debit `cash_available`; credit `contributed_capital`.
10. Preserve the previous session's original starting capital and all history.
11. Enforce one active session per portfolio.
12. Make reset idempotent across the session transition using a durable
    account/portfolio-scoped reset request.

Reset must be one atomic target-database transaction. If any step fails,
the previous session remains active and the new session/book/funding
transaction must not be partially committed.

## 8. Authorization policy

Target authorization matrix:

| Role | Paper Trading | Manual Top-up | Reset |
|---|---|---|---|
| Owner | Allowed | Allowed | Allowed |
| Admin | Allowed | Allowed | Allowed |
| Trader | Allowed | Denied | Denied |
| Viewer | Read-only | Denied | Denied |

Authorization must be checked against trusted server-side identity,
active membership, account ownership, and selected portfolio scope.

The central RBAC and resource-authorization policy has been reconciled
for the target role matrix. Targeted RBAC/resource-authorization tests
were reported passing (137 tests).

This is policy/test evidence only. It does not establish that financial
routes or runtime financial mutation services already enforce this policy.
Every future financial service must perform trusted server-side
authorization again inside its database transaction.

## 9. Accounting rules

Existing default fee rate: 0.001 (0.1%), subject to verified server-side
configuration and versioning.

- Every financial mutation must create a balanced double-entry journal transaction.
- Debit and credit totals must match exactly in the book currency.
- Preserve fixed-precision decimal accounting; binary floating point is
  never authoritative.
- Preserve entry-time fee configuration on each position.
- Record exit-time fee configuration explicitly.
- Preserve accounting version and market metadata/filter reference for
  historical calculations.
- Spot cost basis and Futures locked margin use their respective
  accounting categories and rules.
- `realized_pnl` records gains as credit balances and losses as debit balances.
- `fee_expense` records trading fees as debit balances.
- Initial funding and manual top-ups credit `contributed_capital`.
- Futures liquidation behaviour must fail closed where not implemented.
- Do not fabricate balances, prices, fees, P&L, journal postings, or
  exchange responses.
- Unrealized P&L and equity remain unavailable unless based on verified,
  fresh market prices and a documented valuation method.

## 10. Idempotency requirements

At minimum, protect:

- Initial account, portfolio, session, ledger book, and chart-of-accounts provisioning.
- Position opening and closing.
- Every journal transaction posting.
- Manual Top-up.
- Portfolio Reset, including retries after the active session changes.
- Compensating/reversal journal operations.
- Legacy import and ownership-claim operations.

An identical retry in the same authenticated identity and operation
scope must return the original result without creating another journal
transaction or financial effect.

A reused key with a different canonical request fingerprint must fail.
Database uniqueness and transaction locking must enforce the scope;
client-side checks alone are insufficient.

## 11. Legacy database migration safety

Known legacy database:

- Database: zak_trading_view.
- Applied Alembic revision: 75a1242c020f.
- Legacy tables: portfolios, trading_sessions, ledger_entries, analysis_records.
- Spot portfolio ID: 1.
- Futures portfolio ID: 2.
- Initial/current balances: 1,000 USDT each.
- Legacy sessions: IDs 1 and 2.
- Legacy initial ledger entries: IDs 1 and 2.

Known current application schema uses UUID-based accounts, portfolios,
and sessions. Its current migration head does not establish compatibility
with the legacy applied revision.

Migration requirements:

1. Recover or otherwise formally account for the applied legacy revision.
2. Produce explicit integer-to-UUID mapping records.
3. Preserve original ledger history and timestamps.
4. Preserve portfolio/session relationships.
5. Reconcile imported ledger balances.
6. Verify foreign keys, uniqueness, and ownership constraints.
7. Test migration and restoration on an isolated database.
8. Define rollback and recovery procedures.
9. Never delete or reseed existing financial records to bypass incompatibility.

Do not execute alembic upgrade head against the existing database until
the migration chain and mapping have been independently verified and approved.

### 11.1 Auth0 identity and legacy ownership claim

The project owner has confirmed the intended ownership outcome:

- Legacy Spot portfolio ID 1 is to be associated with the verified
  Auth0 user.
- Legacy Futures portfolio ID 2 is to be associated with the same
  verified Auth0 user.
- Existing balances, ledger entries, sessions, and financial history
  must be preserved.

Identity binding requirements:

1. Resolve the target application user only through the verified
   Auth0 identity represented by the exact issuer and subject pair.
2. Email address matching must never be used to automatically link
   the legacy financial records to an application user.
3. The application user UUID must come from the server-side verified
   identity resolution. A client-supplied user UUID is never authority.
4. The target Trading Account must set owner_user_id to that resolved
   user UUID.
5. Exactly one active Owner membership must match the Trading Account
   owner, in accordance with existing database integrity constraints.
6. An existing active account or ownership conflict must stop the
   claim and require explicit reconciliation. No automatic account
   merge or reassignment is permitted.

Claim authorization requirements:

1. A successful Auth0 login alone does not authorize a legacy claim.
2. The claim must require a controlled, one-time, server-side
   authorization grant approved by the deployment owner.
3. The grant must be scoped to the intended legacy source dataset and
   the exact verified Auth0 issuer and subject before ownership is
   committed.
4. The grant must be single-use, protected against replay, and
   excluded from application logs and client-visible responses.
5. The claim endpoint must derive the user identity from the
   authenticated server-side session. It must not trust client-supplied
   owner_user_id, user_id, account_id, or portfolio IDs.
6. Claim attempts must be idempotent and concurrency-safe.
7. A claim for an already-claimed source dataset must fail closed.

Legacy mapping requirements:

The migration must retain a permanent, auditable mapping between:

- Legacy portfolio ID and new Trading Account UUID.
- Legacy portfolio ID and new Paper Portfolio UUID.
- Legacy session ID and new Paper Session UUID.
- Legacy ledger entry ID and the imported journal transaction UUID.
  Every debit/credit posting generated from that source entry must be
  traceable to the mapped journal transaction and its source mapping.

The mapping must also record the source database identity, source
Alembic revision, migration run identifier, and source snapshot
fingerprint. Original legacy identifiers and timestamps must remain
available for traceability. Mapping records must not be silently
rewritten or reused for another owner.

### 11.2 Login provisioning guard and migration execution

The current OIDC login orchestration resolves or provisions the user
and then invokes ensure_default_account_with_initial_portfolios().
That behaviour must be reconciled with the pending legacy claim.

Required behaviour:

1. A pending legacy claim must prevent automatic creation of a
   competing empty default Trading Account.
2. The login flow must not create duplicate Spot/Futures portfolios
   while a legacy ownership claim or migration is pending.
3. Default account provisioning may proceed only after the legacy
   claim has completed successfully, or after an explicit and
   auditable decision that the account is not using legacy data.
4. Claim and provisioning decisions must be serialized so concurrent
   login or claim requests cannot create conflicting accounts.
5. A failed or incomplete claim must not silently fall through to
   ordinary default account creation.

Migration execution safety:

1. Treat the legacy database as a read-only source during extraction.
2. Capture and verify a consistent source snapshot before import.
3. Perform import testing against an isolated target database.
4. Do not assume a transaction can atomically commit across the
   separate legacy and V2 databases.
5. Stage and validate the complete import before committing target
   financial records.
6. Reconcile every imported portfolio balance against its imported
   ledger history before accepting the migration.
7. Preserve original session status, timestamps, ledger identifiers,
   and portfolio relationships.
8. An interrupted or repeated migration must be safely detectable
   and must not duplicate financial records.
9. Rollback must be tested before target commit. After an accepted
   financial import, recovery must use a documented forward-recovery
   procedure rather than destructive downgrade or deletion.
10. Never run V2 upgrade head against the legacy database.

These are approved design requirements, not implementation claims.
The ownership-claim endpoint, provisioning guard, mapping records,
migration process, and their tests remain unimplemented until
separately verified.


### 11.3 Legacy mapping manifest and import identity

The legacy database does not provide the V2 UUID-based account,
portfolio, and session identity model. The migration must create
explicit target identities and preserve a permanent source mapping.

Target ownership structure:

- One new Trading Account UUID is created for the verified Auth0 user.
- Legacy Spot portfolio ID 1 maps to one new Spot Paper Portfolio UUID.
- Legacy Futures portfolio ID 2 maps to one new Futures Paper Portfolio UUID.
- Both Paper Portfolios reference the same Trading Account UUID.
- Each legacy session maps to its own Paper Session UUID.
- Each legacy ledger entry maps to its own imported
  `paper_ledger_transactions` UUID.
  Every generated `paper_ledger_entries` posting must reference that
  journal transaction and the permanent source mapping record.

The Spot and Futures portfolios must never share a ledger book,
chart-of-account rows, cash snapshot, or session.

Required mapping record attributes:

- Mapping record UUID.
- Source dataset identifier.
- Source database identity.
- Source Alembic revision.
- Source snapshot SHA-256 fingerprint.
- Migration run UUID.
- Source table name.
- Original legacy primary key.
- Target entity type.
- Target entity UUID.
- Target Trading Account UUID, where applicable.
- Source row SHA-256 fingerprint.
- Import timestamp.
- Mapping status.

Mapping constraints:

1. A source dataset/table/primary-key combination can be mapped only once.
2. A target entity mapping cannot be silently reassigned to another
   source record.
3. After an import is accepted, mapping identity fields must be
   immutable, including source dataset/table/primary key, target
   entity type and UUID, and target ownership identifiers.
   Permitted lifecycle changes before acceptance must be recorded
   in append-only audit history. Accepted mappings must never be
   reassigned or silently rewritten.
4. A retry must recognize existing mappings and must not duplicate
   target records.
5. Both legacy portfolio mappings must resolve to the same target
   Trading Account UUID.
6. Every session mapping must resolve to its correct target portfolio.
7. Every legacy ledger-entry mapping must resolve to the correct target
   ledger book and imported journal transaction; all generated postings
   must reference that same book and valid chart-of-account categories.

8. Mapping validation must reject missing, duplicate, orphaned, or
   contradictory relationships.

Source snapshot manifest:

The migration manifest must record:

- Source database identity and applied Alembic revision.
- Snapshot creation timestamp and consistency method.
- A SHA-256 fingerprint of a deterministic canonical export.
- Per-table source row counts.
- Per-table source content fingerprints.
- Migration tool/version identifier.
- Migration run identifier.

The canonical export must use deterministic table ordering,
primary-key ordering, and stable decimal/timestamp serialization.
A database name or row count alone is not a sufficient snapshot
fingerprint.

Legacy source coverage:

- portfolios
- trading_sessions
- ledger_entries
- analysis_records

The analysis_records table must be included in the source inventory
and row-count manifest. Its previously observed zero-row state must
be rechecked against the actual migration snapshot. If records exist
at migration time, they must be preserved or explicitly accounted
for in a separately approved retention plan; they must not be
silently omitted.

The migration must not invent a legacy account ID where none exists.
The new Trading Account is a target identity created for the
verified Auth0 user, and its UUID must be recorded consistently
in both portfolio mapping records.

These mapping and manifest structures are target design requirements.
Their ORM models, database constraints, migration implementation,
and executable tests remain outstanding until verified.

### 11.4 One-time legacy ownership grant lifecycle

The legacy ownership claim requires explicit deployment-owner
authorization. A successful Auth0 login is not sufficient.

Grant creation:

1. Grant creation must be a controlled server-side operator action.
2. It must not be available through an unauthenticated or ordinary
   user-facing API endpoint.
3. The grant must be bound to the exact verified Auth0 issuer and
   subject, intended legacy source dataset, and approved source
   snapshot fingerprint.
4. The grant must have a unique identifier, creation timestamp,
   expiry timestamp, and auditable operator approval reference.
5. The grant secret must be generated using a cryptographically
   secure random generator.
6. Only a cryptographic hash of the grant secret may be persisted.
7. The raw secret must never be written to application logs,
   database audit text, or client-visible responses.
8. Grant creation must not itself change account ownership or
   import financial records.

Grant states:

- PENDING: approved and available for the exact claim.
- CONSUMED: atomically consumed when its authorized claim
  reservation is committed.
- REVOKED: invalidated by an authorized operator before consumption.
- EXPIRED: no longer valid after its expiry timestamp.

Migration claim states are separate from grant states:

- RESERVED: grant consumed and unique claim operation reserved.
- IMPORTING: source records are being imported into controlled
  target staging structures.
- VALIDATING: mappings, relationships, balances, and integrity
  constraints are being verified.
- ACCEPTED: all required validation and reconciliation succeeded.
- RECOVERY_REQUIRED: interrupted or failed work requires
  controlled recovery before acceptance.
- REJECTED: claim cannot proceed because of an unresolved
  authorization, identity, ownership, or source-integrity conflict.

Claim reservation:

1. Resolve the user from the authenticated server-side session.
2. Resolve the verified Auth0 issuer and subject from trusted
   identity records.
3. Lock the relevant source-dataset claim record and grant record
   inside one database transaction.
4. Verify that the grant is PENDING and has not expired or
   been revoked.
5. Verify exact issuer and subject equality.
6. Verify exact source dataset identity and snapshot fingerprint.
7. Verify that the source dataset has not already been claimed
   or reserved by another operation.
8. Verify that the target user has no conflicting active account
   or ownership assignment.
9. Reject client-supplied owner, user, account, portfolio,
   session, or migration identity as an authority source.
10. Create a unique durable claim operation identifier.
11. Consume the grant and create the RESERVED claim record
    atomically.
12. A failed reservation transaction must leave neither a
    consumed grant nor a partial claim record.

Import and acceptance:

1. The RESERVED claim must prevent competing default-account
   provisioning for the verified identity.
2. Import must use the exact source snapshot fingerprint bound
   to the consumed grant.
3. Import must be idempotent under the durable claim operation ID.
4. Imported records must first be staged and validated.
5. The migration must not assume cross-database atomicity.
6. All portfolio, session, ledger, and mapping relationships
   must be checked before acceptance.
7. Every imported portfolio balance must reconcile against its
   imported ledger history.
8. Final acceptance must be a controlled transaction that
   records the ACCEPTED state and final ownership association.
9. The target must not be represented as fully migrated or
   accepted while validation remains incomplete.

Concurrency and replay protection:

- Only one transaction may successfully consume a grant.
- Only one active claim operation may reserve a source dataset.
- Unique database constraints must enforce these invariants.
- A repeated completed request may retrieve its existing result
  only after the same authenticated identity and operation scope
  have been verified.
- A consumed grant cannot authorize a different claim operation.
- An interrupted operation must resume using its existing claim
  record, never by reusing the consumed grant.
- A different user, issuer, subject, dataset, or snapshot must
  never reuse the grant or claim operation.
- Expired or revoked grants must not be reactivated implicitly.
- Recovery must be operator-controlled, auditable, and scoped
  to the original claim operation.
- Recovery must not duplicate imported financial records.

Provisioning and recovery:

- The login provisioning guard must remain active for RESERVED,
  IMPORTING, VALIDATING, RECOVERY_REQUIRED, and REJECTED claims.
- A failed, rejected, or interrupted claim must not fall through
  to ordinary default-account provisioning.
- A REJECTED claim must remain blocked until a separate, explicit,
  audited operator decision confirms that no legacy dataset applies
  or that the ownership conflict has been formally resolved.
- A rejected claim must not be reset to PENDING or reused with its
  consumed grant. Further attempts require a separately authorized
  and auditable resolution process.
- Recovery must either resume the original operation safely or
  leave it blocked for explicit operator reconciliation.
- No automatic account merge, owner reassignment, or destructive
  downgrade is permitted.
- Final ownership acceptance must not occur before successful
  import validation and ledger reconciliation.

Audit requirements:

Record grant identifier, operator approval reference, exact
identity binding, source dataset identifier, snapshot fingerprint,
claim operation identifier, migration state transitions,
creation and expiry timestamps, consumption timestamp,
validation result, acceptance timestamp, and final state.

Never record the raw grant secret.

Failed reservation attempts must retain a non-secret audit outcome
and correlation identifier where appropriate. A failed reservation
must not create a consumed grant or partial claim. Audit records
must not disclose another user's grant or claim existence through
client-visible responses.

Security and failure handling:

- Do not reveal whether another user's grant or claim exists.
- Do not expose grant secrets or internal authorization details
  in API error responses.
- Apply authenticated-session and CSRF protections to any
  browser-initiated claim operation.
- A conflict or ambiguous ownership state must stop the claim.
- Grant authorization and migration acceptance are separate
  events and must remain separately auditable.

These are target design requirements. Grant models, claim models,
operator tooling, database constraints, import staging, recovery
services, provisioning integration, and executable security tests
remain unimplemented until independently verified.

## 12. Implementation boundaries

- No real exchange order endpoints.
- No real exchange trading credentials for execution.
- Paper-only execution mode.
- No browser localStorage as authoritative financial storage.
- No silent financial repair.
- No unverified live market valuation.
- No destructive migration or mass data rewrite.

## 13. Outstanding implementation verification

The following remain to be implemented or verified:

- Exact ORM model definitions.
- Exact SQL constraints and indexes.
- Database-level ledger immutability enforcement.
- Ledger/snapshot reconciliation implementation.
- Transactional accounting services.
- Idempotency and concurrency controls.
- Runtime financial-service authorization enforcement and transactional revalidation.
- Session archive/reset service.
- Manual Top-up service.
- Auth0 legacy-claim authorization grant lifecycle and replay protection.
- Login provisioning guard and concurrent claim/provisioning protection.
- Permanent legacy source-to-target mapping records.
- Legacy migration import, reconciliation, and recovery tooling.
- Legacy migration mapping and compatibility.
- Unit, integration, security, and regression tests.

## 14. Acceptance status

This document is a design contract draft.

It does not by itself establish that any database schema, migration,
accounting service, or runtime security boundary has been implemented.

Phase 2 can only be marked complete after source implementation,
migration compatibility review, and executable test evidence.


## 17. Canonical snapshot storage decision

The immutable double-entry journal remains the only financial source of truth.

The canonical persisted balance snapshot is the per-account snapshot row
for each ledger account. There must not be a second independently writable
available-cash balance in paper_ledger_books.

Available cash is read from the snapshot for the cash_available account.
If a book-level available-cash field is exposed by an API or query, it is
a derived projection of that one canonical account snapshot and must not
be independently persisted or independently updated.

The six account snapshots belong to exactly one ledger book and reconcile
to that book's posted journal entries. Snapshot changes and journal
postings commit atomically. Any mismatch fails closed.

This decision supersedes any earlier wording that could be interpreted
as requiring two independent persisted cash snapshots.

## 18. Exact Decimal and PostgreSQL numeric constraints

Use PostgreSQL NUMERIC without a guessed fixed precision/scale typmod.
The application and database must enforce the approved domain limits:

- Decimal text length must not exceed 128 characters.
- Decimal scale must not exceed 36.
- Absolute finite value must not exceed 1e36.
- Binary floating-point values are prohibited for financial calculations.
- Values must be parsed and validated as exact Decimal values.
- Exchange tickSize and stepSize are the authoritative market increments.
- pricePrecision and quantityPrecision must not replace filter validation.

Every persisted financial NUMERIC value must reject NaN, Infinity and
-Infinity. Database constraints must enforce finite values, scale and
magnitude, in addition to application-level validation.

A suitable finite-value condition for PostgreSQL NUMERIC is:
value > '-Infinity'::numeric AND value < 'Infinity'::numeric.
The actual constraint must be applied to every relevant monetary,
quantity, price, fee, balance and journal amount column, with the
appropriate sign and zero rules for that column.

## 19. Phase 2 implementation boundary

Phase 2 defines and verifies architecture, data contracts, constraints,
migration compatibility, identity ownership policy, accounting rules,
and acceptance criteria.

It does not claim that Phase 3 financial tables, journal posting services,
or runtime financial tests have already been implemented.

No live exchange order, real-money execution, production database
migration, or legacy financial import is authorized by this document.
