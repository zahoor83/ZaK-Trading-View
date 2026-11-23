"""Create short-lived server-side OIDC authorization transactions."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0003_oidc_auth_transactions"
down_revision = "0002_portfolios_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oidc_auth_transactions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column("state_hash", sa.String(64), nullable=False),
        sa.Column("nonce_hash", sa.String(64), nullable=False),
        sa.Column("browser_binding_hash", sa.String(64), nullable=False),
        sa.Column("code_verifier_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("code_verifier_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("encryption_key_id", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "state_hash",
            name="uq_oidc_auth_transactions_state_hash",
        ),
        sa.CheckConstraint(
            "state_hash ~ '^[0-9a-f]{64}$'",
            name="ck_oidc_transactions_state_hash",
        ),
        sa.CheckConstraint(
            "nonce_hash ~ '^[0-9a-f]{64}$'",
            name="ck_oidc_transactions_nonce_hash",
        ),
        sa.CheckConstraint(
            "browser_binding_hash ~ '^[0-9a-f]{64}$'",
            name="ck_oidc_transactions_browser_hash",
        ),
        sa.CheckConstraint(
            "octet_length(code_verifier_nonce) = 12",
            name="ck_oidc_transactions_gcm_nonce_length",
        ),
        sa.CheckConstraint(
            "octet_length(code_verifier_ciphertext) > 16",
            name="ck_oidc_transactions_ciphertext_length",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_oidc_transactions_expiry",
        ),
        sa.CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= created_at",
            name="ck_oidc_transactions_consumed_time",
        ),
    )

    op.create_index(
        "ix_oidc_auth_transactions_expires_at",
        "oidc_auth_transactions",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_oidc_auth_transactions_expires_at",
        table_name="oidc_auth_transactions",
    )
    op.drop_table("oidc_auth_transactions")
