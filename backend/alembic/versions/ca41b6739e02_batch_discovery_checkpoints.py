"""Persist discovery checkpoints and fence worker ownership."""

import sqlalchemy as sa

from alembic import op

revision = "ca41b6739e02"
down_revision = "f3c91a2e8b10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("gmail_sent_records", sa.Column("contact_keys_json", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("search_preferences", sa.Column("excluded_recipients_json", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("mailbox_connections", sa.Column("sent_sync_checkpoint_json", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("batches", sa.Column("discovery_stage", sa.String(40)))
    op.add_column("batches", sa.Column("checkpoint_json", sa.JSON()))
    op.add_column("batches", sa.Column("lease_token", sa.String(36)))
    op.add_column("batches", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    op.add_column("batches", sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("gmail_sent_records", "contact_keys_json")
    op.drop_column("search_preferences", "excluded_recipients_json")
    op.drop_column("mailbox_connections", "sent_sync_checkpoint_json")
    for column in ("consecutive_failures", "lease_expires_at", "lease_token", "checkpoint_json", "discovery_stage"):
        op.drop_column("batches", column)
