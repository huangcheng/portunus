"""site_health: EWMA upstream latency for latency-aware placement

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-14 21:30:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "site_health",
        sa.Column("ewma_upstream_ms", sa.Float(), nullable=True),
    )
    op.add_column(
        "site_health",
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "site_health",
        sa.Column("last_sample_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("site_health", "last_sample_at")
    op.drop_column("site_health", "sample_count")
    op.drop_column("site_health", "ewma_upstream_ms")
