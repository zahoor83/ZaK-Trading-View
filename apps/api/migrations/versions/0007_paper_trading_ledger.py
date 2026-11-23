"""Paper trading ledger schema and database integrity safeguards.

Revision ID: 0007_paper_trading_ledger
Revises: 0006_internal_service_nonce_uuid

Creates the paper-trading financial schema, indexes, and integrity triggers.
"""

from alembic import op

revision = "0007_paper_trading_ledger"
down_revision = "0006_internal_service_nonce_uuid"
branch_labels = None
depends_on = None

TABLE_NAMES = ['paper_ledger_books',
 'paper_execution_requests',
 'paper_ledger_accounts',
 'paper_ledger_transactions',
 'paper_positions',
 'paper_ledger_account_snapshots',
 'paper_ledger_entries']
TABLE_DDL = ["CREATE TABLE paper_ledger_books (\n\tid UUID DEFAULT gen_random_uuid() NOT NULL, \n\taccount_id UUID NOT NULL, \n\tportfolio_id UUID NOT NULL, \n\tsession_id UUID NOT NULL, \n\tmarket_type VARCHAR(10) NOT NULL, \n\tstatus VARCHAR(20) DEFAULT 'active' NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tarchived_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tCONSTRAINT fk_paper_ledger_books_account_portfolio FOREIGN KEY(account_id, portfolio_id) REFERENCES paper_portfolios (account_id, id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_paper_ledger_books_account_portfolio_session FOREIGN KEY(account_id, portfolio_id, session_id) REFERENCES paper_sessions (account_id, portfolio_id, id) ON DELETE RESTRICT, \n\tCONSTRAINT uq_paper_ledger_books_portfolio_session UNIQUE (account_id, portfolio_id, session_id), \n\tCONSTRAINT uq_paper_ledger_books_id_market UNIQUE (id, market_type), \n\tCONSTRAINT uq_paper_ledger_books_account_id_id UNIQUE (account_id, id), \n\tCONSTRAINT ck_paper_ledger_books_market CHECK (market_type IN ('spot', 'futures')), \n\tCONSTRAINT ck_paper_ledger_books_archive_state CHECK ((status = 'active' AND archived_at IS NULL) OR (status = 'archived' AND archived_at IS NOT NULL)), \n\tFOREIGN KEY(account_id) REFERENCES trading_accounts (id) ON DELETE RESTRICT\n)", "CREATE TABLE paper_execution_requests (\n\tid UUID DEFAULT gen_random_uuid() NOT NULL, \n\tscope_type VARCHAR(12) NOT NULL, \n\taccount_id UUID NOT NULL, \n\tbook_id UUID, \n\tportfolio_id UUID, \n\toperation_type VARCHAR(24) NOT NULL, \n\tidempotency_key VARCHAR(128) NOT NULL, \n\trequest_fingerprint VARCHAR(64) NOT NULL, \n\tstatus VARCHAR(12) DEFAULT 'completed' NOT NULL, \n\tresponse_data JSONB DEFAULT '{}'::jsonb NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tcompleted_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT fk_paper_execution_requests_account_book FOREIGN KEY(account_id, book_id) REFERENCES paper_ledger_books (account_id, id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_paper_execution_requests_account_portfolio FOREIGN KEY(account_id, portfolio_id) REFERENCES paper_portfolios (account_id, id) ON DELETE RESTRICT, \n\tCONSTRAINT ck_paper_execution_requests_scope CHECK ((scope_type = 'book' AND book_id IS NOT NULL AND portfolio_id IS NULL) OR (scope_type = 'portfolio' AND portfolio_id IS NOT NULL AND book_id IS NULL)), \n\tCONSTRAINT ck_paper_execution_requests_operation_scope CHECK ((scope_type = 'book' AND operation_type IN ('trade', 'journal')) OR (scope_type = 'portfolio' AND operation_type IN ('reset', 'manual_top_up'))), \n\tCONSTRAINT ck_paper_execution_requests_key_length CHECK (length(idempotency_key) BETWEEN 1 AND 128), \n\tCONSTRAINT ck_paper_execution_requests_fingerprint CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'), \n\tCONSTRAINT ck_paper_execution_requests_completed CHECK (status = 'completed')\n)", "CREATE TABLE paper_ledger_accounts (\n\tid UUID DEFAULT gen_random_uuid() NOT NULL, \n\tbook_id UUID NOT NULL, \n\tmarket_type VARCHAR(10) NOT NULL, \n\tcode VARCHAR(40) NOT NULL, \n\tnormal_side VARCHAR(6) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT fk_paper_ledger_accounts_book_market FOREIGN KEY(book_id, market_type) REFERENCES paper_ledger_books (id, market_type) ON DELETE RESTRICT, \n\tCONSTRAINT uq_paper_ledger_accounts_book_code UNIQUE (book_id, code), \n\tCONSTRAINT uq_paper_ledger_accounts_book_id UNIQUE (book_id, id), \n\tCONSTRAINT ck_paper_ledger_accounts_market CHECK (market_type IN ('spot', 'futures')), \n\tCONSTRAINT ck_paper_ledger_accounts_code CHECK (code IN ('cash_available', 'spot_position_cost', 'futures_margin_locked', 'realized_pnl', 'fee_expense', 'contributed_capital')), \n\tCONSTRAINT ck_paper_ledger_accounts_normal_side CHECK ((code IN ('realized_pnl', 'contributed_capital') AND normal_side = 'credit') OR (code IN ('cash_available', 'spot_position_cost', 'futures_margin_locked', 'fee_expense') AND normal_side = 'debit'))\n)", "CREATE TABLE paper_ledger_transactions (\n\tid UUID DEFAULT gen_random_uuid() NOT NULL, \n\tbook_id UUID NOT NULL, \n\tidempotency_key VARCHAR(128) NOT NULL, \n\trequest_fingerprint VARCHAR(64) NOT NULL, \n\ttransaction_type VARCHAR(32) NOT NULL, \n\tstatus VARCHAR(12) DEFAULT 'draft' NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tposted_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_paper_ledger_transactions_book_id UNIQUE (book_id, id), \n\tCONSTRAINT uq_paper_ledger_transactions_book_key UNIQUE (book_id, idempotency_key), \n\tCONSTRAINT ck_paper_ledger_transactions_key_length CHECK (length(idempotency_key) BETWEEN 1 AND 128), \n\tCONSTRAINT ck_paper_ledger_transactions_fingerprint CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'), \n\tCONSTRAINT ck_paper_ledger_transactions_type CHECK (transaction_type IN ('initial_funding', 'spot_buy', 'spot_sell', 'futures_open', 'futures_close', 'manual_top_up', 'correction')), \n\tCONSTRAINT ck_paper_ledger_transactions_status CHECK (status IN ('draft', 'posted')), \n\tCONSTRAINT ck_paper_ledger_transactions_posting_time CHECK ((status = 'draft' AND posted_at IS NULL) OR (status = 'posted' AND posted_at IS NOT NULL)), \n\tFOREIGN KEY(book_id) REFERENCES paper_ledger_books (id) ON DELETE RESTRICT\n)", "CREATE TABLE paper_positions (\n\tid UUID DEFAULT gen_random_uuid() NOT NULL, \n\tbook_id UUID NOT NULL, \n\tmarket_type VARCHAR(10) NOT NULL, \n\tsymbol VARCHAR(64) NOT NULL, \n\tside VARCHAR(5) NOT NULL, \n\tleverage_snapshot NUMERIC, \n\tinitial_quantity NUMERIC NOT NULL, \n\tremaining_quantity NUMERIC NOT NULL, \n\tentry_price NUMERIC NOT NULL, \n\tremaining_cost_basis NUMERIC, \n\tmargin_locked NUMERIC, \n\tentry_fee_original NUMERIC NOT NULL, \n\tentry_fee_posted NUMERIC NOT NULL, \n\tentry_fee_rate NUMERIC NOT NULL, \n\texit_fee_rate NUMERIC NOT NULL, \n\tfee_config_snapshot JSONB NOT NULL, \n\tsymbol_metadata_snapshot JSONB NOT NULL, \n\trealized_pnl NUMERIC DEFAULT 0 NOT NULL, \n\tstatus VARCHAR(10) DEFAULT 'open' NOT NULL, \n\topened_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tclosed_at TIMESTAMP WITH TIME ZONE, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT fk_paper_positions_book_market FOREIGN KEY(book_id, market_type) REFERENCES paper_ledger_books (id, market_type) ON DELETE RESTRICT, \n\tCONSTRAINT ck_paper_positions_market CHECK (market_type IN ('spot', 'futures')), \n\tCONSTRAINT ck_paper_positions_side CHECK (side IN ('long', 'short')), \n\tCONSTRAINT ck_paper_positions_market_fields CHECK ((market_type = 'spot' AND side = 'long' AND remaining_cost_basis IS NOT NULL AND margin_locked IS NULL AND leverage_snapshot IS NULL) OR (market_type = 'futures' AND remaining_cost_basis IS NULL AND margin_locked IS NOT NULL AND leverage_snapshot IS NOT NULL AND leverage_snapshot > 0 AND leverage_snapshot < 1e36 AND scale(leverage_snapshot) <= 36)), \n\tCONSTRAINT ck_paper_positions_symbol CHECK (length(symbol) BETWEEN 1 AND 64), \n\tCONSTRAINT ck_paper_positions_symbol_metadata CHECK (jsonb_typeof(symbol_metadata_snapshot) = 'object' AND symbol_metadata_snapshot <> '{}'::jsonb), \n\tCONSTRAINT ck_paper_positions_initial_quantity CHECK (initial_quantity > 0 AND initial_quantity < 1e36 AND scale(initial_quantity) <= 36), \n\tCONSTRAINT ck_paper_positions_remaining_quantity CHECK (remaining_quantity >= 0 AND remaining_quantity < 1e36 AND scale(remaining_quantity) <= 36 AND remaining_quantity <= initial_quantity), \n\tCONSTRAINT ck_paper_positions_entry_price CHECK (entry_price > 0 AND entry_price < 1e36 AND scale(entry_price) <= 36), \n\tCONSTRAINT ck_paper_positions_cost_margin CHECK ((remaining_cost_basis IS NULL OR (remaining_cost_basis >= 0 AND remaining_cost_basis < 1e36 AND scale(remaining_cost_basis) <= 36)) AND (margin_locked IS NULL OR (margin_locked >= 0 AND margin_locked < 1e36 AND scale(margin_locked) <= 36))), \n\tCONSTRAINT ck_paper_positions_entry_fees CHECK (entry_fee_original >= 0 AND entry_fee_original < 1e36 AND scale(entry_fee_original) <= 36 AND entry_fee_posted >= 0 AND entry_fee_posted < 1e36 AND scale(entry_fee_posted) <= 2), \n\tCONSTRAINT ck_paper_positions_fee_rates CHECK (entry_fee_rate >= 0 AND entry_fee_rate < 1e36 AND scale(entry_fee_rate) <= 36 AND exit_fee_rate >= 0 AND exit_fee_rate < 1e36 AND scale(exit_fee_rate) <= 36), \n\tCONSTRAINT ck_paper_positions_realized_pnl CHECK (realized_pnl > -1e36 AND realized_pnl < 1e36 AND scale(realized_pnl) <= 36), \n\tCONSTRAINT ck_paper_positions_lifecycle CHECK ((status = 'open' AND closed_at IS NULL AND remaining_quantity > 0) OR (status = 'closed' AND closed_at IS NOT NULL AND remaining_quantity = 0))\n)", 'CREATE TABLE paper_ledger_account_snapshots (\n\tid UUID DEFAULT gen_random_uuid() NOT NULL, \n\tbook_id UUID NOT NULL, \n\tledger_account_id UUID NOT NULL, \n\tbalance NUMERIC DEFAULT 0 NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT fk_paper_ledger_snapshots_account_book FOREIGN KEY(book_id, ledger_account_id) REFERENCES paper_ledger_accounts (book_id, id) ON DELETE RESTRICT, \n\tCONSTRAINT uq_paper_ledger_snapshots_account UNIQUE (book_id, ledger_account_id), \n\tCONSTRAINT ck_paper_ledger_snapshots_balance CHECK (balance > -1e36 AND balance < 1e36 AND scale(balance) <= 36)\n)', "CREATE TABLE paper_ledger_entries (\n\tid UUID DEFAULT gen_random_uuid() NOT NULL, \n\tbook_id UUID NOT NULL, \n\ttransaction_id UUID NOT NULL, \n\tledger_account_id UUID NOT NULL, \n\tdirection VARCHAR(6) NOT NULL, \n\tamount NUMERIC NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT fk_paper_ledger_entries_transaction_book FOREIGN KEY(book_id, transaction_id) REFERENCES paper_ledger_transactions (book_id, id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_paper_ledger_entries_account_book FOREIGN KEY(book_id, ledger_account_id) REFERENCES paper_ledger_accounts (book_id, id) ON DELETE RESTRICT, \n\tCONSTRAINT ck_paper_ledger_entries_direction CHECK (direction IN ('debit', 'credit')), \n\tCONSTRAINT ck_paper_ledger_entries_amount CHECK (amount > 0 AND amount < 1e36 AND scale(amount) <= 36)\n)"]
INDEX_DDL = ['CREATE INDEX ix_paper_ledger_books_account_portfolio ON paper_ledger_books (account_id, '
 'portfolio_id)',
 'CREATE INDEX ix_paper_execution_requests_created ON paper_execution_requests (created_at)',
 'CREATE UNIQUE INDEX uq_paper_execution_requests_book_key ON paper_execution_requests (book_id, '
 "idempotency_key) WHERE scope_type = 'book'",
 'CREATE UNIQUE INDEX uq_paper_execution_requests_portfolio_key ON paper_execution_requests '
 "(portfolio_id, idempotency_key) WHERE scope_type = 'portfolio'",
 'CREATE INDEX ix_paper_ledger_transactions_book_created ON paper_ledger_transactions (book_id, '
 'created_at)',
 "CREATE UNIQUE INDEX uq_paper_ledger_transactions_one_initial_funding ON paper_ledger_transactions (book_id) WHERE transaction_type = 'initial_funding'",
 'CREATE INDEX ix_paper_positions_book_status ON paper_positions (book_id, status)',
 'CREATE INDEX ix_paper_positions_book_symbol ON paper_positions (book_id, symbol)',
 'CREATE INDEX ix_paper_ledger_entries_account ON paper_ledger_entries (book_id, '
 'ledger_account_id)',
 'CREATE INDEX ix_paper_ledger_entries_transaction ON paper_ledger_entries (book_id, '
 'transaction_id)',
]


def _current_schema_sql():
    connection = op.get_bind()
    schema = connection.exec_driver_sql(
        "SELECT current_schema()"
    ).scalar_one_or_none()

    if not isinstance(schema, str) or not schema:
        raise RuntimeError("Cannot resolve migration target schema.")

    return connection.dialect.identifier_preparer.quote(schema)


INTEGRITY_DDL = [
r"""CREATE FUNCTION __SCHEMA__.paper_ledger_guard_transaction_lifecycle()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, __SCHEMA__
AS $function$
DECLARE
    v_book_status text;
    v_entry_count bigint;
    v_debit_count bigint;
    v_credit_count bigint;
    v_debit_total numeric;
    v_credit_total numeric;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Ledger transactions cannot be deleted'
            USING ERRCODE = '23514';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF NEW.status IS DISTINCT FROM 'draft'
           OR NEW.posted_at IS NOT NULL THEN
            RAISE EXCEPTION 'New ledger transactions must start as draft'
                USING ERRCODE = '23514';
        END IF;
    ELSE
        IF OLD.status IS DISTINCT FROM 'draft'
           OR NEW.status IS DISTINCT FROM 'posted' THEN
            RAISE EXCEPTION 'Only draft-to-posted transition is allowed'
                USING ERRCODE = '23514';
        END IF;

        IF NEW.id IS DISTINCT FROM OLD.id
           OR NEW.book_id IS DISTINCT FROM OLD.book_id
           OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
           OR NEW.request_fingerprint IS DISTINCT FROM OLD.request_fingerprint
           OR NEW.transaction_type IS DISTINCT FROM OLD.transaction_type
           OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'Ledger transaction identity is immutable'
                USING ERRCODE = '23514';
        END IF;
    END IF;

    SELECT status
      INTO v_book_status
      FROM __SCHEMA__.paper_ledger_books
     WHERE id = NEW.book_id
     FOR SHARE;

    IF NOT FOUND OR v_book_status IS DISTINCT FROM 'active' THEN
        RAISE EXCEPTION 'Ledger book must be active'
            USING ERRCODE = '23514';
    END IF;

    IF TG_OP = 'UPDATE' THEN
        SELECT
            COUNT(*),
            COUNT(*) FILTER (WHERE direction = 'debit'),
            COUNT(*) FILTER (WHERE direction = 'credit'),
            COALESCE(SUM(amount) FILTER (WHERE direction = 'debit'), 0),
            COALESCE(SUM(amount) FILTER (WHERE direction = 'credit'), 0)
        INTO
            v_entry_count,
            v_debit_count,
            v_credit_count,
            v_debit_total,
            v_credit_total
        FROM __SCHEMA__.paper_ledger_entries
        WHERE book_id = NEW.book_id
          AND transaction_id = NEW.id;

        IF v_entry_count < 2
           OR v_debit_count = 0
           OR v_credit_count = 0
           OR v_debit_total IS DISTINCT FROM v_credit_total THEN
            RAISE EXCEPTION 'Cannot post an unbalanced ledger transaction'
                USING ERRCODE = '23514';
        END IF;

        NEW.posted_at := clock_timestamp();
    END IF;

    RETURN NEW;
END;
$function$""",

r"""CREATE FUNCTION __SCHEMA__.paper_ledger_guard_entry_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, __SCHEMA__
AS $function$
DECLARE
    v_transaction_status text;
    v_book_status text;
    v_book_market_type text;
    v_account_code text;
    v_account_market_type text;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'Ledger entries are immutable'
            USING ERRCODE = '23514';
    END IF;

    SELECT status
      INTO v_transaction_status
      FROM __SCHEMA__.paper_ledger_transactions
     WHERE id = NEW.transaction_id
       AND book_id = NEW.book_id
     FOR UPDATE;

    IF NOT FOUND OR v_transaction_status IS DISTINCT FROM 'draft' THEN
        RAISE EXCEPTION 'Entries can only be added to draft transactions'
            USING ERRCODE = '23514';
    END IF;

    SELECT status, market_type
      INTO v_book_status, v_book_market_type
      FROM __SCHEMA__.paper_ledger_books
     WHERE id = NEW.book_id
     FOR SHARE;

    IF NOT FOUND OR v_book_status IS DISTINCT FROM 'active' THEN
        RAISE EXCEPTION 'Ledger book must be active'
            USING ERRCODE = '23514';
    END IF;

    SELECT code, market_type
      INTO v_account_code, v_account_market_type
      FROM __SCHEMA__.paper_ledger_accounts
     WHERE book_id = NEW.book_id
       AND id = NEW.ledger_account_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Ledger account does not exist in this book'
            USING ERRCODE = '23514';
    END IF;

    IF v_account_market_type IS DISTINCT FROM v_book_market_type THEN
        RAISE EXCEPTION 'Ledger account market does not match book market'
            USING ERRCODE = '23514';
    END IF;

    IF (v_book_market_type = 'spot'
        AND v_account_code = 'futures_margin_locked')
       OR
       (v_book_market_type = 'futures'
        AND v_account_code = 'spot_position_cost') THEN
        RAISE EXCEPTION 'Cross-market ledger account posting is prohibited'
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END;
$function$""",

r"""CREATE FUNCTION __SCHEMA__.paper_ledger_assert_transaction_posted()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, __SCHEMA__
AS $function$
DECLARE
    v_status text;
BEGIN
    SELECT status
      INTO v_status
      FROM __SCHEMA__.paper_ledger_transactions
     WHERE id = NEW.id
       AND book_id = NEW.book_id;

    IF NOT FOUND OR v_status IS DISTINCT FROM 'posted' THEN
        RAISE EXCEPTION 'Draft ledger transaction cannot survive commit'
            USING ERRCODE = '23514';
    END IF;

    RETURN NULL;
END;
$function$""",

r"""CREATE TRIGGER trg_paper_ledger_transaction_lifecycle
BEFORE INSERT OR UPDATE OR DELETE
ON __SCHEMA__.paper_ledger_transactions
FOR EACH ROW
EXECUTE FUNCTION __SCHEMA__.paper_ledger_guard_transaction_lifecycle()""",

r"""CREATE TRIGGER trg_paper_ledger_entry_immutable
BEFORE INSERT OR UPDATE OR DELETE
ON __SCHEMA__.paper_ledger_entries
FOR EACH ROW
EXECUTE FUNCTION __SCHEMA__.paper_ledger_guard_entry_insert()""",

r"""CREATE CONSTRAINT TRIGGER trg_paper_ledger_transaction_must_post
AFTER INSERT OR UPDATE
ON __SCHEMA__.paper_ledger_transactions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION __SCHEMA__.paper_ledger_assert_transaction_posted()""",

r"""CREATE FUNCTION __SCHEMA__.paper_ledger_guard_snapshot_write()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, __SCHEMA__
AS $function$
DECLARE
    v_create_owner oid;
    v_apply_owner oid;
BEGIN
    IF pg_trigger_depth() < 2 THEN
        RAISE EXCEPTION
            'Ledger snapshots can only be changed by approved writer functions'
            USING ERRCODE = '42501';
    END IF;

    SELECT writer.proowner
      INTO v_create_owner
      FROM pg_catalog.pg_proc AS writer
      JOIN pg_catalog.pg_namespace AS writer_schema
        ON writer_schema.oid = writer.pronamespace
     WHERE writer_schema.nspname = TG_TABLE_SCHEMA
       AND writer.proname = 'paper_ledger_create_account_snapshot'
       AND writer.pronargs = 0
       AND writer.prosecdef;

    SELECT writer.proowner
      INTO v_apply_owner
      FROM pg_catalog.pg_proc AS writer
      JOIN pg_catalog.pg_namespace AS writer_schema
        ON writer_schema.oid = writer.pronamespace
     WHERE writer_schema.nspname = TG_TABLE_SCHEMA
       AND writer.proname = 'paper_ledger_apply_posted_transaction_to_snapshots'
       AND writer.pronargs = 0
       AND writer.prosecdef;

    IF v_create_owner IS NULL
       OR v_apply_owner IS NULL
       OR v_create_owner IS DISTINCT FROM v_apply_owner
       OR current_user::regrole::oid IS DISTINCT FROM v_create_owner THEN
        RAISE EXCEPTION
            'Snapshot mutation is not executing under the approved writer owner'
            USING ERRCODE = '42501';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;

    RETURN NEW;
END;
$function$""",

r"""CREATE TRIGGER trg_paper_ledger_snapshot_write_guard
BEFORE INSERT OR UPDATE OR DELETE
ON __SCHEMA__.paper_ledger_account_snapshots
FOR EACH ROW
EXECUTE FUNCTION __SCHEMA__.paper_ledger_guard_snapshot_write()""",

r"""CREATE FUNCTION __SCHEMA__.paper_ledger_create_account_snapshot()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, __SCHEMA__
AS $function$
BEGIN
    INSERT INTO __SCHEMA__.paper_ledger_account_snapshots (
        book_id,
        ledger_account_id,
        balance,
        updated_at
    )
    VALUES (
        NEW.book_id,
        NEW.id,
        0,
        clock_timestamp()
    );

    RETURN NEW;
END;
$function$""",

r"""CREATE TRIGGER trg_paper_ledger_account_create_snapshot
AFTER INSERT
ON __SCHEMA__.paper_ledger_accounts
FOR EACH ROW
EXECUTE FUNCTION __SCHEMA__.paper_ledger_create_account_snapshot()""",


r"""CREATE FUNCTION __SCHEMA__.paper_ledger_apply_posted_transaction_to_snapshots()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, __SCHEMA__
AS $function$
DECLARE
    v_account record;
    v_delta numeric;
    v_snapshot_id uuid;
    v_updated_count bigint;
BEGIN
    IF OLD.status IS DISTINCT FROM 'draft'
       OR NEW.status IS DISTINCT FROM 'posted' THEN
        RAISE EXCEPTION 'Snapshot update requires draft-to-posted transition'
            USING ERRCODE = '23514';
    END IF;

    FOR v_account IN
        SELECT
            entry.ledger_account_id,
            account.normal_side,
            COALESCE(
                SUM(entry.amount) FILTER (
                    WHERE entry.direction = 'debit'
                ),
                0
            ) AS debit_total,
            COALESCE(
                SUM(entry.amount) FILTER (
                    WHERE entry.direction = 'credit'
                ),
                0
            ) AS credit_total
        FROM __SCHEMA__.paper_ledger_entries AS entry
        JOIN __SCHEMA__.paper_ledger_accounts AS account
          ON account.book_id = entry.book_id
         AND account.id = entry.ledger_account_id
        WHERE entry.book_id = NEW.book_id
          AND entry.transaction_id = NEW.id
        GROUP BY
            entry.ledger_account_id,
            account.normal_side
        ORDER BY entry.ledger_account_id
    LOOP
        IF v_account.normal_side = 'debit' THEN
            v_delta := v_account.debit_total - v_account.credit_total;

        ELSIF v_account.normal_side = 'credit' THEN
            v_delta := v_account.credit_total - v_account.debit_total;

        ELSE
            RAISE EXCEPTION 'Unsupported ledger account normal side'
                USING ERRCODE = '23514';
        END IF;

        SELECT snapshot.id
          INTO v_snapshot_id
          FROM __SCHEMA__.paper_ledger_account_snapshots AS snapshot
         WHERE snapshot.book_id = NEW.book_id
           AND snapshot.ledger_account_id = v_account.ledger_account_id
         FOR UPDATE;

        IF NOT FOUND THEN
            RAISE EXCEPTION
                'Missing ledger snapshot for account %',
                v_account.ledger_account_id
                USING ERRCODE = '23514';
        END IF;

        UPDATE __SCHEMA__.paper_ledger_account_snapshots
           SET balance = balance + v_delta,
               updated_at = clock_timestamp()
         WHERE id = v_snapshot_id
           AND book_id = NEW.book_id
           AND ledger_account_id = v_account.ledger_account_id;

        GET DIAGNOSTICS v_updated_count = ROW_COUNT;

        IF v_updated_count <> 1 THEN
            RAISE EXCEPTION
                'Ledger snapshot update failed for account %',
                v_account.ledger_account_id
                USING ERRCODE = '23514';
        END IF;
    END LOOP;

    RETURN NEW;
END;
$function$""",

r"""CREATE TRIGGER trg_paper_ledger_apply_posted_transaction
AFTER UPDATE OF status
ON __SCHEMA__.paper_ledger_transactions
FOR EACH ROW
WHEN (OLD.status = 'draft' AND NEW.status = 'posted')
EXECUTE FUNCTION __SCHEMA__.paper_ledger_apply_posted_transaction_to_snapshots()""",

r"""REVOKE ALL ON TABLE __SCHEMA__.paper_ledger_account_snapshots FROM PUBLIC""",

r"""REVOKE ALL ON FUNCTION __SCHEMA__.paper_ledger_create_account_snapshot() FROM PUBLIC""",

r"""REVOKE ALL ON FUNCTION __SCHEMA__.paper_ledger_apply_posted_transaction_to_snapshots() FROM PUBLIC""",


    r"""CREATE FUNCTION __SCHEMA__.paper_ledger_guard_account_immutable()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, __SCHEMA__
AS $function$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        RAISE EXCEPTION 'Ledger account identity and normal side are immutable'
            USING ERRCODE = '23514';
    END IF;

    RAISE EXCEPTION 'Unsupported ledger account operation'
        USING ERRCODE = '23514';
END;
$function$""",

    r"""CREATE FUNCTION __SCHEMA__.paper_ledger_assert_book_has_six_accounts()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, __SCHEMA__
AS $function$
DECLARE
    v_account_count bigint;
    v_distinct_code_count bigint;
BEGIN
    SELECT COUNT(*), COUNT(DISTINCT code)
      INTO v_account_count, v_distinct_code_count
      FROM __SCHEMA__.paper_ledger_accounts
     WHERE book_id = NEW.id;

    IF v_account_count <> 6 OR v_distinct_code_count <> 6 THEN
        RAISE EXCEPTION
            'Ledger book must have exactly six distinct mandatory accounts; found % accounts and % distinct codes',
            v_account_count,
            v_distinct_code_count
            USING ERRCODE = '23514';
    END IF;

    RETURN NULL;
END;
$function$""",

    r"""CREATE TRIGGER trg_paper_ledger_account_immutable
BEFORE UPDATE OR DELETE
ON __SCHEMA__.paper_ledger_accounts
FOR EACH ROW
EXECUTE FUNCTION __SCHEMA__.paper_ledger_guard_account_immutable()""",

    r"""CREATE CONSTRAINT TRIGGER trg_paper_ledger_book_account_completeness
AFTER INSERT
ON __SCHEMA__.paper_ledger_books
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION __SCHEMA__.paper_ledger_assert_book_has_six_accounts()""",

    r"""CREATE FUNCTION __SCHEMA__.paper_execution_request_immutable()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, __SCHEMA__
AS $function$
BEGIN
    RAISE EXCEPTION 'Paper execution requests are immutable'
        USING ERRCODE = '23514';

    RETURN NULL;
END;
$function$""",

    r"""CREATE TRIGGER trg_paper_execution_request_immutable
BEFORE UPDATE OR DELETE
ON __SCHEMA__.paper_execution_requests
FOR EACH ROW
EXECUTE FUNCTION __SCHEMA__.paper_execution_request_immutable()""",

    r"""CREATE TRIGGER trg_paper_execution_request_no_truncate
BEFORE TRUNCATE
ON __SCHEMA__.paper_execution_requests
FOR EACH STATEMENT
EXECUTE FUNCTION __SCHEMA__.paper_execution_request_immutable()""",
]


INTEGRITY_DROP_DDL = [
    "DROP TRIGGER IF EXISTS trg_paper_ledger_snapshot_write_guard ON __SCHEMA__.paper_ledger_account_snapshots",
    "DROP TRIGGER IF EXISTS trg_paper_ledger_apply_posted_transaction ON __SCHEMA__.paper_ledger_transactions",
    "DROP TRIGGER IF EXISTS trg_paper_ledger_account_create_snapshot ON __SCHEMA__.paper_ledger_accounts",

    "DROP TRIGGER IF EXISTS trg_paper_ledger_transaction_must_post ON __SCHEMA__.paper_ledger_transactions",
    "DROP TRIGGER IF EXISTS trg_paper_ledger_entry_immutable ON __SCHEMA__.paper_ledger_entries",
    "DROP TRIGGER IF EXISTS trg_paper_ledger_transaction_lifecycle ON __SCHEMA__.paper_ledger_transactions",
    "DROP TRIGGER IF EXISTS trg_paper_ledger_book_account_completeness ON __SCHEMA__.paper_ledger_books",
    "DROP TRIGGER IF EXISTS trg_paper_ledger_account_immutable ON __SCHEMA__.paper_ledger_accounts",
    "DROP FUNCTION IF EXISTS __SCHEMA__.paper_ledger_guard_snapshot_write()",
    "DROP FUNCTION IF EXISTS __SCHEMA__.paper_ledger_apply_posted_transaction_to_snapshots()",
    "DROP FUNCTION IF EXISTS __SCHEMA__.paper_ledger_create_account_snapshot()",
    "DROP FUNCTION IF EXISTS __SCHEMA__.paper_ledger_assert_transaction_posted()",
    "DROP FUNCTION IF EXISTS __SCHEMA__.paper_ledger_guard_entry_insert()",
    "DROP FUNCTION IF EXISTS __SCHEMA__.paper_ledger_guard_transaction_lifecycle()",
    "DROP FUNCTION IF EXISTS __SCHEMA__.paper_ledger_assert_book_has_six_accounts()",
    "DROP FUNCTION IF EXISTS __SCHEMA__.paper_ledger_guard_account_immutable()",
    'DROP TRIGGER IF EXISTS trg_paper_execution_request_no_truncate ON __SCHEMA__.paper_execution_requests',
    'DROP TRIGGER IF EXISTS trg_paper_execution_request_immutable ON __SCHEMA__.paper_execution_requests',
    'DROP FUNCTION IF EXISTS __SCHEMA__.paper_execution_request_immutable()',
]


def upgrade() -> None:
    for statement in TABLE_DDL:
        op.execute(statement)
    for statement in INDEX_DDL:
        op.execute(statement)

    schema_sql = _current_schema_sql()
    for statement in INTEGRITY_DDL:
        op.execute(statement.replace("__SCHEMA__", schema_sql))


def downgrade() -> None:
    schema_name = op.get_bind().exec_driver_sql(
        "SELECT current_schema()"
    ).scalar_one_or_none()

    if not isinstance(schema_name, str) or not schema_name:
        raise RuntimeError("Cannot resolve downgrade target schema.")

    op.execute("""
    DO $$
    DECLARE
        table_name text;
        row_count bigint;
        target_schema text;
    BEGIN
        target_schema := current_schema();

        IF target_schema IS NULL OR target_schema = '' THEN
            RAISE EXCEPTION 'Cannot resolve downgrade target schema.';
        END IF;

        FOREACH table_name IN ARRAY ARRAY[
            'paper_execution_requests',
            'paper_positions',
            'paper_ledger_entries',
            'paper_ledger_transactions',
            'paper_ledger_account_snapshots',
            'paper_ledger_accounts',
            'paper_ledger_books'
        ]
        LOOP
            EXECUTE format('SELECT COUNT(*) FROM %I.%I', target_schema, table_name)
                INTO row_count;
            IF row_count > 0 THEN
                RAISE EXCEPTION
                    'Refusing paper-ledger downgrade: table % contains % rows',
                    table_name, row_count;
            END IF;
        END LOOP;
    END
    $$;
    """)

    schema_sql = _current_schema_sql()
    for statement in INTEGRITY_DROP_DDL:
        op.execute(statement.replace("__SCHEMA__", schema_sql))

    for table_name in reversed(TABLE_NAMES):
        op.drop_table(table_name, schema=schema_name)
