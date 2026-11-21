"""add gemini bundle import provenance columns

Revision ID: f3c91a2e8b10
Revises: ad04bf741887
Create Date: 2026-11-19 23:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3c91a2e8b10"
down_revision: str | Sequence[str] | None = "ad04bf741887"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("batches", sa.Column("import_origin", sa.String(length=40), nullable=False, server_default=""))
    op.add_column("batches", sa.Column("source_archive_sha256", sa.String(length=64), nullable=False, server_default=""))
    op.add_column("batches", sa.Column("source_archive_storage_key", sa.String(length=500), nullable=False, server_default=""))
    op.create_index("ix_batches_source_archive_sha256", "batches", ["source_archive_sha256"], unique=False)

    op.add_column("applications", sa.Column("import_origin", sa.String(length=40), nullable=False, server_default=""))
    op.add_column("applications", sa.Column("import_item_hash", sa.String(length=64), nullable=False, server_default=""))
    op.create_index("ix_applications_import_item_hash", "applications", ["import_item_hash"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_applications_import_item_hash", table_name="applications")
    op.drop_column("applications", "import_item_hash")
    op.drop_column("applications", "import_origin")

    op.drop_index("ix_batches_source_archive_sha256", table_name="batches")
    op.drop_column("batches", "source_archive_storage_key")
    op.drop_column("batches", "source_archive_sha256")
    op.drop_column("batches", "import_origin")
