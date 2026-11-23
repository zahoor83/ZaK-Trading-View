"""Create PostgreSQL-backed internal service request replay records."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0005_internal_service_nonces"
down_revision = "0004_oidc_idempotency_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "internal_service_request_nonces",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column("service_id", sa.String(128), nullable=False),
        sa.Column("key_id", sa.String(64), nullable=False),
        sa.Column("nonce_hash", sa.String(64), nullable=False),
        sa.Column(
            "signed_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "service_id",
            "key_id",
            "nonce_hash",
            name="uq_internal_service_nonce_identity",
        ),
        sa.CheckConstraint(
            "length(service_id) BETWEEN 1 AND 128",
            name="ck_internal_service_nonce_service_length",
        ),
        sa.CheckConstraint(
            "service_id = btrim(service_id)",
            name="ck_internal_service_nonce_service_trimmed",
        ),
        sa.CheckConstraint(
            "key_id ~ '^[A-Za-z0-9._-]{1,64}$'",
            name="ck_internal_service_nonce_key_id",
        ),
        sa.CheckConstraint(
            "nonce_hash ~ '^[0-9a-f]{64}$'",
            name="ck_internal_service_nonce_hash",
        ),
        sa.CheckConstraint(
            "expires_at > signed_at",
            name="ck_internal_service_nonce_expiry_after_signed_at",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_internal_service_nonce_expiry_after_created_at",
        ),
    )

    op.create_index(
        "ix_internal_service_request_nonces_expires_at",
        "internal_service_request_nonces",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_internal_service_request_nonces_expires_at",
        table_name="internal_service_request_nonces",
    )
    op.drop_table("internal_service_request_nonces")