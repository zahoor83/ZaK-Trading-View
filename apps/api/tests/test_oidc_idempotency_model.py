import importlib.util
from pathlib import Path

from sqlalchemy import UniqueConstraint

from app.database.base import Base
from app.models.oidc import OIDCAuthTransaction, OIDCIdempotencyRecord


def test_idempotency_model_is_registered_in_metadata():
    assert "oidc_idempotency_records" in Base.metadata.tables


def test_idempotency_model_has_service_scoped_unique_constraint():
    constraints = [
        item
        for item in OIDCIdempotencyRecord.__table__.constraints
        if isinstance(item, UniqueConstraint)
    ]

    assert any(
        tuple(column.name for column in item.columns)
        == ("service_id", "idempotency_key_hash")
        for item in constraints
    )


def test_transaction_foreign_key_uses_set_null():
    column = OIDCIdempotencyRecord.__table__.c.transaction_id
    foreign_keys = list(column.foreign_keys)

    assert column.nullable is True
    assert len(foreign_keys) == 1
    assert foreign_keys[0].target_fullname == "oidc_auth_transactions.id"
    assert foreign_keys[0].ondelete == "SET NULL"


def test_encrypted_response_fields_are_required():
    table = OIDCIdempotencyRecord.__table__

    assert table.c.response_nonce.nullable is False
    assert table.c.response_ciphertext.nullable is False
    assert table.c.encryption_key_id.nullable is False


def test_expiry_index_exists():
    indexes = {
        item.name for item in OIDCIdempotencyRecord.__table__.indexes
    }

    assert "ix_oidc_idempotency_records_expires_at" in indexes


def test_existing_transaction_model_remains_registered():
    assert "oidc_auth_transactions" in Base.metadata.tables
    assert OIDCAuthTransaction.__tablename__ == "oidc_auth_transactions"


def test_model_is_exported_from_app_models_package():
    import app.models as models

    assert models.OIDCIdempotencyRecord is OIDCIdempotencyRecord


def test_migration_revision_chain():
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0004_oidc_idempotency_records.py"
    )

    spec = importlib.util.spec_from_file_location(
        "migration_0004_oidc_idempotency_records",
        migration_path,
    )
    assert spec is not None and spec.loader is not None

    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.revision == "0004_oidc_idempotency_records"
    assert migration.down_revision == "0003_oidc_auth_transactions"
    assert callable(migration.upgrade)
    assert callable(migration.downgrade)