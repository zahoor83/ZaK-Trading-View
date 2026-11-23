# Paper Trading - Phase 2 Final Architecture Decisions

Status: FINAL ARCHITECTURE DECISION RECORD
Project: ZaK Trading View
Architecture: Option A - Double-entry ledger
Database authority: PostgreSQL
Currency: USDT
Initial virtual capital: 1,000 USDT per portfolio

## 1. Scope and non-negotiable requirements

1. Spot and Futures are separate financial portfolios.
2. Each portfolio starts with exactly 1,000 USDT.
3. Funds cannot be transferred between portfolios.
4. PostgreSQL is the authoritative financial state.
5. The immutable double-entry journal is the financial source of truth.
6. Snapshots are transactional read models, never independent authorities.
7. Browser refresh cannot reset, replenish, or recreate balances.
8. No real funds, exchange orders, or live execution are permitted.
9. All financial calculations use exact decimal arithmetic.
10. Missing, stale, inconsistent, or unverifiable financial inputs fail closed.

## 2. Approved data model

The following tables form the financial persistence boundary:

- paper_ledger_books
- paper_ledger_accounts
- paper_ledger_transactions
- paper_ledger_entries
- paper_ledger_account_snapshots
- paper_positions
- paper_execution_requests

A ledger book belongs to exactly one account, portfolio, and session.
A new session receives a new ledger book. Spot and Futures never share a book.

Every financial ownership relationship must be protected by composite
foreign keys that include the owning book or account where applicable.

## 3. Six mandatory ledger account categories

Every ledger book must contain exactly one account for each category:

1. cash_available
   Normal balance: debit.

2. spot_position_cost
   Normal balance: debit.

3. futures_margin_locked
   Normal balance: debit.

4. realized_pnl
   Gains are credit balances; losses are debit balances.

5. fee_expense
   Normal balance: debit.

6. contributed_capital
   Normal balance: credit.

Category uniqueness alone is insufficient. Atomic provisioning and a
deferred database integrity check or equivalent trusted database mechanism
must guarantee all six categories exist at transaction commit.

Spot books cannot post to futures_margin_locked.
Futures books cannot post to spot_position_cost.

## 4. Journal integrity and immutability

Every posted journal transaction must:

- Belong to exactly one ledger book.
- Contain at least two entries.
- Contain at least one debit and one credit.
- Have strictly positive entry amounts.
- Have exactly one of debit or credit populated per entry.
- Satisfy total debits = total credits exactly.
- Keep every entry inside the same book as its transaction and account.

Cross-row balance validation requires a deferred constraint trigger
or a restricted trusted posting mechanism. A simple CHECK constraint
cannot enforce transaction-wide balance.

Posted journal headers and entries are immutable.
UPDATE and DELETE of posted financial records are prohibited.
Corrections require new compensating journal transactions.

All journal and snapshot updates occur in one database transaction.
A failed journal, snapshot, or reconciliation operation rolls back everything.

## 5. Initial funding

Initial Spot funding:

Dr cash_available             1,000 USDT
Cr contributed_capital        1,000 USDT

Initial Futures funding:

Dr cash_available             1,000 USDT
Cr contributed_capital        1,000 USDT

Each portfolio has its own independent initial funding transaction.

## 6. Trade accounting rules

### Spot buy

Dr spot_position_cost         gross purchase cost
Dr fee_expense                entry fee
Cr cash_available             gross cost + entry fee

### Spot sell

Dr cash_available             sale proceeds
Cr spot_position_cost         released cost basis

The difference is posted to realized_pnl:
- Gain: credit realized_pnl.
- Loss: debit realized_pnl.

Exit fees are separately posted:
Dr fee_expense
Cr cash_available

### Futures open

Dr futures_margin_locked      locked margin
Cr cash_available             locked margin

Opening fee:
Dr fee_expense
Cr cash_available

### Futures close

Release margin:
Dr cash_available
Cr futures_margin_locked

Realized profit:
Dr cash_available
Cr realized_pnl

Realized loss:
Dr realized_pnl
Cr cash_available

Exit fee:
Dr fee_expense
Cr cash_available

The realized profit or loss is calculated from the verified position
and execution price. No fabricated mark price or exchange response is allowed.

Position records retain entry fee and the applicable fee/configuration
snapshot so closing calculations do not silently use a changed fee rate.

## 7. Snapshot and reconciliation

Account snapshots are read models derived from journal entries.

The posting transaction must atomically:
1. Validate the journal.
2. Insert journal header and entries.
3. Update affected account snapshots.
4. Record the resulting journal revision or sequence.
5. Verify snapshot reconciliation before commit.

A mismatch fails closed and rolls back the transaction.

Snapshots cannot be used to invent or repair journal history.
Rebuilding a snapshot must be possible from the immutable journal.

## 8. Decimal and database numeric contract

Existing backend symbol filter validation establishes these canonical
input limits:

- Maximum decimal text length: 128 characters.
- Maximum fractional scale: 36.
- Maximum absolute decimal value: 1e36.
- Boolean and binary floating-point inputs are prohibited.

Database financial values use PostgreSQL NUMERIC without a fixed
precision/scale typmod. This avoids implicit typmod rounding.

Database constraints and trusted application validation must enforce:
- finite, valid decimal values;
- fractional scale no greater than 36;
- absolute value no greater than 1e36;
- strictly positive journal entry amounts;
- required nonnegative or positive semantics for each financial field.

Derived notional, margin, fee, P&L, and journal values must also satisfy
the persisted-value bounds. If an exact result exceeds the supported
scale or magnitude, reject the operation. Never silently round to fit.

No floating-point conversion is permitted in authoritative financial
calculations or database persistence.

Market order validation must use exchange filter values:
- PRICE_FILTER.tickSize
- LOT_SIZE.stepSize
- MARKET_LOT_SIZE.stepSize
- minQty and maxQty
- applicable minimum and maximum notional rules

pricePrecision and quantityPrecision are descriptive metadata, not
substitutes for tickSize or stepSize.

## 9. Account provisioning

Both existing account provisioning paths must use one shared,
transaction-bound ledger provisioning service:

- create_account_with_initial_portfolios
- ensure_default_account_with_initial_portfolios

The shared service provisions:
- Spot and Futures portfolios and sessions;
- one independent ledger book per portfolio session;
- six mandatory accounts per book;
- one balanced 1,000 USDT initial funding transaction per book;
- initial account snapshots.

All provisioning occurs inside the existing account transaction.
Any failure rolls back the entire account and financial provisioning.

Alembic model registration must include every new model through
app.models.__init__.py and Base.metadata discovery.

## 10. Foreign-key deletion and history preservation

Existing account-to-portfolio and portfolio-to-session relationships
use cascade deletion.

Financial records must not be deleted through those cascade paths.

New financial references must use restrictive deletion semantics.
Composite ownership foreign keys must prevent cross-account,
cross-portfolio, cross-session, or cross-book references.

Financial history is retained. Account lifecycle should use status
transitions and controlled archival rather than physical deletion
of records referenced by financial history.

## 11. Idempotency

Trade and journal operations:
- idempotency scope includes ledger book and operation key;
- a repeated request returns the original result;
- a conflicting request fingerprint is rejected;
- a failed operation cannot leave partial financial postings.

Reset operations:
- scope includes account, portfolio, and reset operation key;
- idempotency remains valid across the session transition;
- duplicate reset requests cannot create multiple new funded sessions.

Top-up operations:
- use an explicit operation key and request fingerprint;
- duplicate requests cannot credit funds twice.

## 12. Manual top-up

Top-up is allowed only when the selected portfolio cash balance is
less than or equal to 100 USDT.

After explicit user confirmation, credit:

1000 USDT - current available cash

Example:
Current cash: 75 USDT
Top-up: 925 USDT
Result: 1,000 USDT

Journal:
Dr cash_available
Cr contributed_capital

Record an immutable MANUAL_TOP_UP operation.

Top-up does not change startingCapital, close positions, release margin,
or erase session history.

No automatic top-up is permitted during refresh or ordinary reads.

## 13. Portfolio reset

Reset is restricted to the selected portfolio and requires explicit
confirmation.

Reset is blocked while that portfolio has open positions.

Reset must:
1. Lock the selected portfolio and its active session.
2. Verify no open positions or locked margin remain.
3. Archive the existing session and ledger book.
4. Preserve all prior journal and closed-trade history.
5. Create a new session and ledger book.
6. Provision exactly six new ledger accounts.
7. Post a new 1,000 USDT initial funding transaction.
8. Create matching initial snapshots.
9. Commit the transition atomically.

Reset must never silently close positions or release margin.
Spot reset must not affect Futures, and Futures reset must not affect Spot.

## 14. Authorization

Owner/Admin:
- lifecycle reset and manual top-up, subject to explicit confirmation.

Trader:
- paper trade and manage permitted positions.

Viewer:
- read-only.

Every operation must authorize the authenticated account membership
on the server. Client-supplied account, portfolio, session, or book IDs
must not establish ownership.

## 15. Legacy migration safety

The existing 0002 migration refuses to proceed when trading_accounts
already contains records. No automatic portfolio backfill is safe.

The new financial migration must follow revision 0006_internal_service_nonce_uuid.

Do not run the V2 migration chain against the legacy database whose
revision is 75a1242c020f. Do not modify the legacy database on port 5432.

Any legacy financial import requires:
- a separately approved migration plan;
- exact issuer plus subject identity mapping;
- no email-based identity matching;
- explicit owner approval;
- permanent source-to-target mapping;
- duplicate and missing-record detection;
- reconciliation of imported balances and history;
- isolated test evidence before any production use.

No legacy financial records are silently discarded or merged.

## 16. Migration and implementation sequence

1. Add SQLAlchemy models and register them in app.models.
2. Add migration 0007 with restrictive ownership references.
3. Add deferred journal balance and six-account completeness enforcement.
4. Add immutable posted-journal protections.
5. Add the shared transactional ledger provisioning service.
6. Add initial funding and snapshot reconciliation.
7. Add trade journal posting and idempotency.
8. Add position persistence and fee/configuration snapshots.
9. Add reset and top-up lifecycle operations.
10. Add authorization and paper-only execution boundary.
11. Add isolated PostgreSQL migration and integrity tests.
12. Reconcile documentation and acceptance evidence.

Do not implement frontend integration before backend financial authority
and persistence are verified.

## 17. Phase 2 acceptance criteria

Phase 2 architecture is accepted when:

- The approved Option A architecture is documented.
- All seven financial table responsibilities are defined.
- Six account categories and normal balances are fixed.
- Journal balancing and immutability enforcement are specified.
- Exact decimal bounds and fail-closed behavior are fixed.
- Exchange tickSize and stepSize validation is required.
- Account provisioning is unified and atomic.
- Restrictive deletion and ownership constraints are specified.
- Reset, top-up, idempotency, and RBAC contracts are fixed.
- Migration 0007 parent and legacy migration isolation are documented.
- The existing Phase 2 architecture contract is reconciled with this record.
- The final document passes structural and integrity verification.

This record defines architecture only. It does not claim that financial
models, migrations, services, or runtime tests have already been implemented.

## 16. Legacy migration manifest and mapping requirements

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

## 16.1 One-time legacy ownership grant and claim lifecycle

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
