"""Enable tools left disabled by the old probe-default.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-16 18:20:00.000000

Probe used to insert tools with enabled=false until the operator approved each
one. That default is flipped to enabled=true; this backfill turns on tools for
servers that still have *zero* enabled tools (the all-pending shape from the
old default). Servers with a mixed enable/disable set are left alone so
intentional per-tool disables survive.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE tools AS t
        SET enabled = true
        WHERE t.enabled = false
          AND t.server_id IN (
            SELECT server_id
            FROM tools
            GROUP BY server_id
            HAVING SUM(CASE WHEN enabled THEN 1 ELSE 0 END) = 0
          )
        """
    )


def downgrade() -> None:
    # Irreversible data fix — cannot distinguish prior intentional disables.
    pass
