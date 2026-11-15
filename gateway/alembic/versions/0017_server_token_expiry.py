"""servers: token_expires_at + has_oauth_refresh for grant lifetime (F9)

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-15 16:45:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "servers",
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "servers",
        sa.Column(
            "has_oauth_refresh",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Backfill: any server with a vaulted refresh_token can renew without re-auth.
    op.execute(
        """
        UPDATE servers AS s
        SET has_oauth_refresh = true
        FROM vault_entries AS v
        WHERE v.server_id = s.id
          AND v.name = 'portunus.oauth_refresh_token'
        """
    )


def downgrade() -> None:
    op.drop_column("servers", "has_oauth_refresh")
    op.drop_column("servers", "token_expires_at")
