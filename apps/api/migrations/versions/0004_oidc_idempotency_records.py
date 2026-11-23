"""Create independently retained OIDC idempotency records."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0004_oidc_idempotency_records"
down_revision = "0003_oidc_auth_transactions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oidc_idempotency_records",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column("service_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "transaction_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "oidc_auth_transactions.id",
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
        sa.Column("response_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("response_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("encryption_key_id", sa.String(64), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'completed'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "service_id",
            "idempotency_key_hash",
            name="uq_oidc_idempotency_service_key",
        ),
        sa.CheckConstraint(
            "length(service_id) BETWEEN 1 AND 128",
            name="ck_oidc_idempotency_service_length",
        ),
        sa.CheckConstraint(
            "service_id = btrim(service_id)",
            name="ck_oidc_idempotency_service_trimmed",
        ),
        sa.CheckConstraint(
            "idempotency_key_hash ~ '^[0-9a-f]{64}$'",
            name="ck_oidc_idempotency_key_hash",
        ),
        sa.CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_oidc_idempotency_fingerprint",
        ),
        sa.CheckConstraint(
            "octet_length(response_nonce) = 12",
            name="ck_oidc_idempotency_nonce_length",
        ),
        sa.CheckConstraint(
            "octet_length(response_ciphertext) > 16",
            name="ck_oidc_idempotency_ciphertext_length",
        ),
        sa.CheckConstraint(
            "encryption_key_id ~ '^[A-Za-z0-9._-]{1,64}$'",
            name="ck_oidc_idempotency_encryption_key_id",
        ),
        sa.CheckConstraint(
            "status = 'completed'",
            name="ck_oidc_idempotency_status",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_oidc_idempotency_expiry",
        ),
    )

    op.create_index(
        "ix_oidc_idempotency_records_expires_at",
        "oidc_idempotency_records",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_oidc_idempotency_records_expires_at",
        table_name="oidc_idempotency_records",
    )
    op.drop_table("oidc_idempotency_records")