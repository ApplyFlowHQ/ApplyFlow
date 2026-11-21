"""Preserve opaque provider pagination cursors beyond 255 characters."""

import sqlalchemy as sa

from alembic import op

revision = "d2910fb0c136"
down_revision = "ca41b6739e02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column("mailbox_connections", "inbox_cursor", existing_type=sa.String(255), type_=sa.Text())


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        # PostgreSQL rejects this conversion if any saved cursor exceeds the old limit.
        # Avoid truncating opaque cursors, which would silently lose mailbox coverage.
        op.execute("""
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM mailbox_connections WHERE length(inbox_cursor) > 255) THEN
                    RAISE EXCEPTION 'Long mailbox cursor present. Retain the wider column for a code rollback.';
                END IF;
            END $$;
        """)
        op.alter_column("mailbox_connections", "inbox_cursor", existing_type=sa.Text(), type_=sa.String(255))
