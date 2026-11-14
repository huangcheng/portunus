"""servers: recipe_slug, snapshots, health, site ids for TOML catalog

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-14 18:10:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("recipe_slug", sa.String(length=100), nullable=True))
    op.add_column("servers", sa.Column("artifact_snapshot", sa.JSON(), nullable=True))
    op.add_column(
        "servers",
        sa.Column("env_snapshot", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.add_column("servers", sa.Column("manifest_name", sa.String(length=200), nullable=True))
    op.add_column("servers", sa.Column("manifest_sha256", sa.String(length=64), nullable=True))
    op.add_column("servers", sa.Column("preferred_site_id", sa.String(length=100), nullable=True))
    op.add_column("servers", sa.Column("artifact_site_id", sa.String(length=100), nullable=True))
    op.add_column(
        "servers",
        sa.Column("health", sa.String(length=32), nullable=False, server_default="healthy"),
    )
    op.add_column("servers", sa.Column("health_code", sa.String(length=64), nullable=True))
    op.create_index("ix_servers_recipe_slug", "servers", ["recipe_slug"])


def downgrade() -> None:
    op.drop_index("ix_servers_recipe_slug", table_name="servers")
    op.drop_column("servers", "health_code")
    op.drop_column("servers", "health")
    op.drop_column("servers", "artifact_site_id")
    op.drop_column("servers", "preferred_site_id")
    op.drop_column("servers", "manifest_sha256")
    op.drop_column("servers", "manifest_name")
    op.drop_column("servers", "env_snapshot")
    op.drop_column("servers", "artifact_snapshot")
    op.drop_column("servers", "recipe_slug")
