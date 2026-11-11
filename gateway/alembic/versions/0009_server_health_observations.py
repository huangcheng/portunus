"""servers: observation columns for derived health status

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-11 18:30:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("last_probe_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("servers", sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("servers", sa.Column("last_error_code", sa.String(64), nullable=True))
    op.add_column("servers", sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "last_error_at")
    op.drop_column("servers", "last_error_code")
    op.drop_column("servers", "last_ok_at")
    op.drop_column("servers", "last_probe_at")
