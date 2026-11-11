"""catalog_apps: categories + tags for Apps browse/filter

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-10 23:50:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("catalog_apps") as batch:
        batch.add_column(sa.Column("categories", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
        batch.add_column(sa.Column("tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))


def downgrade() -> None:
    with op.batch_alter_table("catalog_apps") as batch:
        batch.drop_column("tags")
        batch.drop_column("categories")
