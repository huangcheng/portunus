"""site_health: learned per-(server, site) network health for auto failover

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-12 22:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "site_health",
        sa.Column("server_id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.String(length=100), nullable=False),
        sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "consecutive_failures", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.ForeignKeyConstraint(
            ["server_id"],
            ["servers.id"],
            name=op.f("fk_site_health_server_id_servers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("server_id", "site_id", name=op.f("pk_site_health")),
    )


def downgrade() -> None:
    op.drop_table("site_health")
