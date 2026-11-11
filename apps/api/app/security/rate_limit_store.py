"""Durable, provider-isolated Binance rate-limit state storage.

This module owns SQLite persistence only. It does not perform network I/O,
parse HTTP headers, sleep, retry requests, or decide whether a request may
be dispatched. The coordinator enforces those policies.
"""

from __future__ import annotations

import math
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final


SCHEMA_VERSION: Final[int] = 1
PROVIDERS: Final[frozenset[str]] = frozenset({"spot", "usdm_futures"})
RATE_LIMIT_STATUSES: Final[frozenset[int]] = frozenset({418, 429})


_INITIALIZATION_LOCK = threading.RLock()


@contextmanager
def _initialization_file_lock(database_path: Path, timeout_seconds: float):
    """Serialize database initialization across operating-system processes."""
    lock_path = database_path.with_name(database_path.name + ".init.lock")
    handle = None
    acquired = False
    deadline = time.monotonic() + timeout_seconds

    try:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\\0")
            handle.flush()

        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(
                        handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                    )
                acquired = True
                break
            except OSError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RateLimitStoreError(
                        "Timed out acquiring rate-limit initialization lock."
                    ) from exc
                time.sleep(min(0.05, remaining))

        yield

    except RateLimitStoreError:
        raise
    except OSError as exc:
        raise RateLimitStoreError(
            "Unable to acquire rate-limit initialization lock."
        ) from exc
    finally:
        if handle is not None:
            if acquired:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()


class RateLimitStoreError(RuntimeError):
    """Raised when durable rate-limit state cannot be trusted or accessed."""


@dataclass(frozen=True, slots=True)
class ProviderRateLimitState:
    """Immutable persisted state for one Binance provider."""

    provider: str
    state: str
    blocked_until_utc: datetime | None
    status_code: int
    created_at_utc: datetime
    updated_at_utc: datetime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_provider(provider: str) -> str:
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise ValueError("Provider must be 'spot' or 'usdm_futures'.")
    return provider


def _to_utc_iso(value: datetime, field_name: str) -> str:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field_name} must be a timezone-aware datetime.")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _from_utc_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise RateLimitStoreError("Stored UTC timestamp is invalid.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RateLimitStoreError("Stored UTC timestamp has no timezone.")
    return parsed.astimezone(timezone.utc)


class SQLiteRateLimitStore:
    """SQLite-backed state store with atomic, cross-process updates.

    The database path must be an absolute filesystem path. In-memory SQLite
    databases are deliberately prohibited because they cannot survive restart.
    Every public operation opens its own connection; SQLite transactions and
    BEGIN IMMEDIATE serialize writers across threads and processes.
    """

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        *,
        busy_timeout_seconds: float = 5.0,
    ) -> None:
        if isinstance(database_path, str) and database_path == ":memory:":
            raise ValueError("An in-memory database is prohibited.")
        try:
            path = Path(database_path).expanduser()
        except (TypeError, ValueError) as exc:
            raise ValueError("A valid SQLite filesystem path is required.") from exc
        if not path.is_absolute():
            raise ValueError("SQLite database path must be absolute.")
        if (
            isinstance(busy_timeout_seconds, bool)
            or not isinstance(busy_timeout_seconds, (int, float))
            or not math.isfinite(busy_timeout_seconds)
            or busy_timeout_seconds <= 0
        ):
            raise ValueError("SQLite busy timeout must be finite and positive.")

        self.database_path = path
        self.busy_timeout_seconds = float(busy_timeout_seconds)
        self._initialization_lock = _INITIALIZATION_LOCK
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                str(self.database_path),
                timeout=self.busy_timeout_seconds,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(
                f"PRAGMA busy_timeout = {int(self.busy_timeout_seconds * 1000)}"
            )
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA foreign_keys = ON")
            return connection
        except (sqlite3.Error, OSError) as exc:
            raise RateLimitStoreError("Unable to open rate-limit SQLite storage.") from exc

    def initialize(self) -> None:
        """Create the schema once, or validate an existing database fail-closed."""
        with self._initialization_lock:
            if self._initialized:
                return

            with _initialization_file_lock(
                self.database_path, self.busy_timeout_seconds
            ):
                connection: sqlite3.Connection | None = None
                try:
                    self.database_path.parent.mkdir(parents=True, exist_ok=True)
                    existed_before = self.database_path.exists()

                    connection = self._connect()

                    if existed_before:
                        current_size = self.database_path.stat().st_size
                        if current_size == 0:
                            raise RateLimitStoreError(
                                "Existing empty rate-limit SQLite database will not be initialized."
                            )

                        integrity = connection.execute("PRAGMA quick_check").fetchone()
                        if integrity is None or integrity[0] != "ok":
                            raise RateLimitStoreError(
                                "Existing rate-limit SQLite database failed integrity check."
                            )

                    journal_mode = connection.execute(
                        "PRAGMA journal_mode = DELETE"
                    ).fetchone()
                    if (
                        journal_mode is None
                        or str(journal_mode[0]).lower() != "delete"
                    ):
                        raise RateLimitStoreError(
                            "Rate-limit SQLite journal mode could not be set to DELETE."
                        )

                    connection.execute("BEGIN IMMEDIATE")

                    version_row = connection.execute("PRAGMA user_version").fetchone()
                    version = int(version_row[0]) if version_row else -1
                    tables = {
                        row[0]
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master "
                            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                        ).fetchall()
                    }

                    if version not in (0, SCHEMA_VERSION):
                        raise RateLimitStoreError(
                            f"Unsupported rate-limit database schema version: {version}."
                        )

                    if version == 0 and existed_before:
                        raise RateLimitStoreError(
                            "Existing unversioned SQLite database will not be initialized."
                        )

                    if version == 0 and tables:
                        raise RateLimitStoreError(
                            "Unversioned non-empty SQLite database will not be modified."
                        )

                    if version == 0:
                        connection.execute(
                            """
                            CREATE TABLE provider_rate_limit_state (
                                provider TEXT PRIMARY KEY
                                    CHECK (provider IN ('spot', 'usdm_futures')),
                                state TEXT NOT NULL
                                    CHECK (state IN ('cooldown', 'indefinite')),
                                blocked_until_utc TEXT,
                                status_code INTEGER NOT NULL
                                    CHECK (status_code IN (418, 429)),
                                created_at_utc TEXT NOT NULL,
                                updated_at_utc TEXT NOT NULL,
                                CHECK (
                                    (state = 'cooldown' AND blocked_until_utc IS NOT NULL)
                                    OR
                                    (state = 'indefinite' AND blocked_until_utc IS NULL)
                                )
                            )
                            """
                        )
                        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    elif "provider_rate_limit_state" not in tables:
                        raise RateLimitStoreError(
                            "Versioned rate-limit database is missing its state table."
                        )

                    if version == SCHEMA_VERSION:
                        columns = {
                            row[1]
                            for row in connection.execute(
                                "PRAGMA table_info(provider_rate_limit_state)"
                            ).fetchall()
                        }
                        expected = {
                            "provider",
                            "state",
                            "blocked_until_utc",
                            "status_code",
                            "created_at_utc",
                            "updated_at_utc",
                        }
                        if columns != expected:
                            raise RateLimitStoreError(
                                "Existing rate-limit database schema does not match."
                            )

                    connection.commit()
                    self._initialized = True
                except RateLimitStoreError:
                    if connection is not None:
                        try:
                            connection.rollback()
                        except sqlite3.Error:
                            pass
                    raise
                except (sqlite3.Error, OSError) as exc:
                    if connection is not None:
                        try:
                            connection.rollback()
                        except sqlite3.Error:
                            pass
                    raise RateLimitStoreError(
                        "Rate-limit SQLite initialization failed."
                    ) from exc
                finally:
                    if connection is not None:
                        connection.close()

    def _read_connection(self) -> sqlite3.Connection:
        self.initialize()
        return self._connect()

    @staticmethod
    def _row_to_state(row: sqlite3.Row | None) -> ProviderRateLimitState | None:
        if row is None:
            return None
        try:
            state = row["state"]
            if state not in ("cooldown", "indefinite"):
                raise RateLimitStoreError("Stored rate-limit state is invalid.")
            blocked_until = _from_utc_iso(row["blocked_until_utc"])
            if state == "cooldown" and blocked_until is None:
                raise RateLimitStoreError("Timed cooldown is missing its deadline.")
            if state == "indefinite" and blocked_until is not None:
                raise RateLimitStoreError("Indefinite block has an unexpected deadline.")
            return ProviderRateLimitState(
                provider=row["provider"],
                state=state,
                blocked_until_utc=blocked_until,
                status_code=int(row["status_code"]),
                created_at_utc=_from_utc_iso(row["created_at_utc"]),  # type: ignore[arg-type]
                updated_at_utc=_from_utc_iso(row["updated_at_utc"]),  # type: ignore[arg-type]
            )
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            if isinstance(exc, RateLimitStoreError):
                raise
            raise RateLimitStoreError("Stored rate-limit record is malformed.") from exc

    def get_state(self, provider: str) -> ProviderRateLimitState | None:
        provider = _normalize_provider(provider)
        connection: sqlite3.Connection | None = None
        try:
            connection = self._read_connection()
            row = connection.execute(
                "SELECT * FROM provider_rate_limit_state WHERE provider = ?",
                (provider,),
            ).fetchone()
            return self._row_to_state(row)
        except RateLimitStoreError:
            raise
        except sqlite3.Error as exc:
            raise RateLimitStoreError("Unable to read provider rate-limit state.") from exc
        finally:
            if connection is not None:
                connection.close()

    def list_states(self) -> tuple[ProviderRateLimitState, ...]:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._read_connection()
            rows = connection.execute(
                "SELECT * FROM provider_rate_limit_state ORDER BY provider"
            ).fetchall()
            states = tuple(self._row_to_state(row) for row in rows)
            return tuple(state for state in states if state is not None)
        except RateLimitStoreError:
            raise
        except sqlite3.Error as exc:
            raise RateLimitStoreError("Unable to list provider rate-limit states.") from exc
        finally:
            if connection is not None:
                connection.close()

    def apply_block(
        self,
        provider: str,
        *,
        status_code: int,
        blocked_until_utc: datetime | None,
        now_utc: datetime | None = None,
    ) -> ProviderRateLimitState:
        """Persist a timed or indefinite block without weakening existing state.

        A None deadline means indefinite. Existing indefinite state can never
        be downgraded by a later timed response. Repeated timed blocks retain
        the later of the existing and newly supplied deadlines.
        """
        provider = _normalize_provider(provider)
        if (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or status_code not in RATE_LIMIT_STATUSES
        ):
            raise ValueError("Rate-limit status must be HTTP 418 or 429.")

        deadline_text = (
            None
            if blocked_until_utc is None
            else _to_utc_iso(blocked_until_utc, "blocked_until_utc")
        )
        now_text = _to_utc_iso(now_utc or _utc_now(), "now_utc")
        connection: sqlite3.Connection | None = None

        try:
            self.initialize()
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            existing_row = connection.execute(
                "SELECT * FROM provider_rate_limit_state WHERE provider = ?",
                (provider,),
            ).fetchone()
            existing = self._row_to_state(existing_row)

            if existing is not None and existing.state == "indefinite":
                state_value = "indefinite"
                final_deadline = None
                created_text = _to_utc_iso(
                    existing.created_at_utc, "created_at_utc"
                )
            elif deadline_text is None:
                state_value = "indefinite"
                final_deadline = None
                created_text = (
                    _to_utc_iso(existing.created_at_utc, "created_at_utc")
                    if existing is not None
                    else now_text
                )
            else:
                state_value = "cooldown"
                if existing is not None and existing.blocked_until_utc is not None:
                    existing_deadline = _to_utc_iso(
                        existing.blocked_until_utc, "blocked_until_utc"
                    )
                    final_deadline = max(existing_deadline, deadline_text)
                else:
                    final_deadline = deadline_text
                created_text = (
                    _to_utc_iso(existing.created_at_utc, "created_at_utc")
                    if existing is not None
                    else now_text
                )

            connection.execute(
                """
                INSERT INTO provider_rate_limit_state (
                    provider, state, blocked_until_utc, status_code,
                    created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    state = excluded.state,
                    blocked_until_utc = excluded.blocked_until_utc,
                    status_code = excluded.status_code,
                    created_at_utc = excluded.created_at_utc,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    provider,
                    state_value,
                    final_deadline,
                    status_code,
                    created_text,
                    now_text,
                ),
            )
            connection.commit()
        except RateLimitStoreError:
            if connection is not None:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            raise
        except sqlite3.Error as exc:
            if connection is not None:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            raise RateLimitStoreError("Unable to persist provider rate-limit block.") from exc
        finally:
            if connection is not None:
                connection.close()

        result = self.get_state(provider)
        if result is None:
            raise RateLimitStoreError("Persisted provider block could not be read back.")
        return result

    def clear_provider(self, provider: str) -> bool:
        """Explicitly clear exactly one provider's persisted state."""
        provider = _normalize_provider(provider)
        connection: sqlite3.Connection | None = None
        try:
            self.initialize()
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM provider_rate_limit_state WHERE provider = ?",
                (provider,),
            )
            deleted = cursor.rowcount > 0
            connection.commit()
            return deleted
        except sqlite3.Error as exc:
            if connection is not None:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            raise RateLimitStoreError("Unable to clear provider rate-limit state.") from exc
        finally:
            if connection is not None:
                connection.close()
