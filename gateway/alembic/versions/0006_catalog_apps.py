"""catalog_apps: operator-editable Apps catalog (oauth/http/webdav/stdio)

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-10 23:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "catalog_apps",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("connection", sa.String(length=16), nullable=False),
        sa.Column("popular", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "connection IN ('oauth', 'http', 'webdav', 'stdio')",
            name="ck_catalog_apps_connection",
        ),
        sa.CheckConstraint("slug = lower(slug)", name="ck_catalog_apps_slug_lower"),
        sa.PrimaryKeyConstraint("id", name="pk_catalog_apps"),
        sa.UniqueConstraint("slug", name="uq_catalog_apps_slug"),
    )


def downgrade() -> None:
    op.drop_table("catalog_apps")
