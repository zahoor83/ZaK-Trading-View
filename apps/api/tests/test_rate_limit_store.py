from __future__ import annotations

import sqlite3
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from app.security.rate_limit_store import (
    ProviderRateLimitState,
    RateLimitStoreError,
    SQLiteRateLimitStore,
)


UTC = timezone.utc


def moment(seconds: int = 0) -> datetime:
    return datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=seconds)


def make_store(tmp_path, name: str = "state.sqlite3") -> SQLiteRateLimitStore:
    return SQLiteRateLimitStore(tmp_path / name)


def _initialize_store_in_process(database_path: str):
    return SQLiteRateLimitStore(database_path).get_state("spot")


def test_initialization_creates_versioned_schema_and_empty_state(tmp_path):
    store = make_store(tmp_path)

    assert store.get_state("spot") is None
    assert store.get_state("usdm_futures") is None
    assert store.database_path.is_file()

    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(provider_rate_limit_state)"
            )
        }
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert journal_mode.lower() == "delete"
    assert columns == {
        "provider",
        "state",
        "blocked_until_utc",
        "status_code",
        "created_at_utc",
        "updated_at_utc",
    }


def test_initialization_converts_existing_wal_database_to_delete_journal_mode(tmp_path):
    store = make_store(tmp_path)
    store.initialize()

    connection = sqlite3.connect(store.database_path)
    try:
        mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    finally:
        connection.close()
    assert mode.lower() == "wal"

    reopened = make_store(tmp_path)
    reopened.initialize()

    connection = sqlite3.connect(reopened.database_path)
    try:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()
    assert mode.lower() == "delete"


def test_timed_cooldown_persists_across_store_instances(tmp_path):
    first = make_store(tmp_path)
    deadline = moment(120)

    saved = first.apply_block(
        "spot",
        status_code=429,
        blocked_until_utc=deadline,
        now_utc=moment(),
    )
    reopened = make_store(tmp_path).get_state("spot")

    assert saved == reopened
    assert reopened is not None
    assert reopened.state == "cooldown"
    assert reopened.blocked_until_utc == deadline


def test_provider_states_are_independent(tmp_path):
    store = make_store(tmp_path)
    store.apply_block(
        "spot", status_code=429, blocked_until_utc=moment(60), now_utc=moment()
    )
    store.apply_block(
        "usdm_futures", status_code=418, blocked_until_utc=None, now_utc=moment()
    )

    spot = store.get_state("spot")
    futures = store.get_state("usdm_futures")

    assert spot is not None and spot.state == "cooldown"
    assert futures is not None and futures.state == "indefinite"


def test_later_timed_deadline_is_retained_and_cannot_be_shortened(tmp_path):
    store = make_store(tmp_path)
    later = moment(300)
    store.apply_block(
        "spot", status_code=429, blocked_until_utc=later, now_utc=moment()
    )
    saved = store.apply_block(
        "spot",
        status_code=418,
        blocked_until_utc=moment(30),
        now_utc=moment(10),
    )

    assert saved.blocked_until_utc == later
    assert saved.created_at_utc == moment()


def test_indefinite_block_cannot_be_downgraded_to_timed(tmp_path):
    store = make_store(tmp_path)
    store.apply_block(
        "spot", status_code=418, blocked_until_utc=None, now_utc=moment()
    )
    saved = store.apply_block(
        "spot",
        status_code=429,
        blocked_until_utc=moment(300),
        now_utc=moment(10),
    )

    assert saved.state == "indefinite"
    assert saved.blocked_until_utc is None


def test_clear_provider_only_clears_selected_provider(tmp_path):
    store = make_store(tmp_path)
    store.apply_block(
        "spot", status_code=429, blocked_until_utc=None, now_utc=moment()
    )
    store.apply_block(
        "usdm_futures", status_code=418, blocked_until_utc=None, now_utc=moment()
    )

    assert store.clear_provider("spot") is True
    assert store.clear_provider("spot") is False
    assert store.get_state("spot") is None
    assert store.get_state("usdm_futures") is not None


@pytest.mark.parametrize("provider", ["", "SPOT", "futures", "binance", None])
def test_rejects_unknown_provider(tmp_path, provider):
    store = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.get_state(provider)


@pytest.mark.parametrize("status_code", [200, 400, 429.0, True, 500])
def test_rejects_non_rate_limit_status_codes(tmp_path, status_code):
    store = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.apply_block(
            "spot",
            status_code=status_code,
            blocked_until_utc=moment(10),
            now_utc=moment(),
        )


@pytest.mark.parametrize(
    "deadline",
    [
        datetime(2026, 11, 4, 12, 0, 0),
        "2026-11-04T12:00:00Z",
    ],
)
def test_rejects_naive_or_non_datetime_deadline(tmp_path, deadline):
    store = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.apply_block(
            "spot", status_code=429, blocked_until_utc=deadline, now_utc=moment()
        )


def test_rejects_in_memory_and_relative_database_paths(tmp_path):
    with pytest.raises(ValueError):
        SQLiteRateLimitStore(":memory:")
    with pytest.raises(ValueError):
        SQLiteRateLimitStore("relative.sqlite3")


def test_rejects_existing_empty_database_instead_of_silently_initializing(tmp_path):
    path = tmp_path / "empty-existing.sqlite3"
    path.write_bytes(b"")

    store = SQLiteRateLimitStore(path)
    with pytest.raises(RateLimitStoreError):
        store.get_state("spot")


def test_rejects_corrupt_database_without_recreating_it(tmp_path):
    path = tmp_path / "corrupt.sqlite3"
    path.write_bytes(b"not a sqlite database")
    original = path.read_bytes()

    with pytest.raises(RateLimitStoreError):
        make_store(tmp_path, path.name).get_state("spot")

    assert path.read_bytes() == original


def test_rejects_existing_unversioned_sqlite_database(tmp_path):
    path = tmp_path / "unversioned-existing.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE transient_table (value TEXT)")
        connection.execute("DROP TABLE transient_table")

    assert path.stat().st_size > 0
    with pytest.raises(RateLimitStoreError):
        SQLiteRateLimitStore(path).get_state("spot")


def test_concurrent_first_initialization_is_safe(tmp_path):
    database_path = tmp_path / "concurrent.sqlite3"

    def initialize_one(_):
        return SQLiteRateLimitStore(database_path).get_state("spot")

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(initialize_one, range(8)))

    assert results == [None] * 8
    assert SQLiteRateLimitStore(database_path).get_state("spot") is None


def test_concurrent_first_initialization_is_safe_across_processes(tmp_path):
    database_path = str(tmp_path / "process-concurrent.sqlite3")

    with ProcessPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(
                _initialize_store_in_process,
                [database_path] * 4,
            )
        )

    assert results == [None] * 4
    assert SQLiteRateLimitStore(database_path).get_state("spot") is None


def test_rejects_unknown_schema_version(tmp_path):
    path = tmp_path / "future-version.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 99")

    with pytest.raises(RateLimitStoreError):
        make_store(tmp_path, path.name).get_state("spot")


def test_database_constraints_reject_invalid_provider_state_and_status(tmp_path):
    store = make_store(tmp_path)
    store.initialize()

    with sqlite3.connect(store.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO provider_rate_limit_state
                VALUES ('other', 'cooldown', '2026-11-04T12:01:00Z',
                        429, '2026-11-04T12:00:00Z', '2026-11-04T12:00:00Z')
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO provider_rate_limit_state
                VALUES ('spot', 'indefinite', '2026-11-04T12:01:00Z',
                        429, '2026-11-04T12:00:00Z', '2026-11-04T12:00:00Z')
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO provider_rate_limit_state
                VALUES ('spot', 'indefinite', NULL,
                        200, '2026-11-04T12:00:00Z', '2026-11-04T12:00:00Z')
                """
            )


def test_list_states_returns_provider_order(tmp_path):
    store = make_store(tmp_path)
    store.apply_block(
        "usdm_futures", status_code=418, blocked_until_utc=None, now_utc=moment()
    )
    store.apply_block(
        "spot", status_code=429, blocked_until_utc=None, now_utc=moment()
    )

    assert [state.provider for state in store.list_states()] == [
        "spot",
        "usdm_futures",
    ]


def test_state_record_is_immutable(tmp_path):
    store = make_store(tmp_path)
    saved = store.apply_block(
        "spot", status_code=429, blocked_until_utc=None, now_utc=moment()
    )

    assert isinstance(saved, ProviderRateLimitState)
    with pytest.raises((AttributeError, TypeError)):
        saved.state = "cooldown"