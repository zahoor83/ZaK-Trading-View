"""Add a database-side UUID default for internal service nonce records."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0006_internal_service_nonce_uuid"
down_revision = "0005_internal_service_nonces"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "internal_service_request_nonces",
        "id",
        existing_type=postgresql.UUID(as_uuid=True),
        existing_nullable=False,
        server_default=sa.text("gen_random_uuid()"),
    )


def downgrade() -> None:
    op.alter_column(
        "internal_service_request_nonces",
        "id",
        existing_type=postgresql.UUID(as_uuid=True),
        existing_nullable=False,
        server_default=None,
    )