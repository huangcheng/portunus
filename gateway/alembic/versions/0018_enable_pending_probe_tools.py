"""Enable tools left disabled by the old probe-default.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-16 18:20:00.000000

Probe used to insert tools with enabled=false until the operator approved each
one. That default is flipped to enabled=true; this backfill turns on tools for
servers that still have *zero* enabled tools (the all-pending shape from the
old default), and skips any tool that has an audit ``tool.disable`` event so
intentional full-server quarantines are not silently reopened.

Caveat: ``PORTUNUS_AUDIT_RETENTION_DAYS`` may have purged old disable events, so
very old intentional disables on all-disabled servers could still be re-enabled.
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
          AND NOT EXISTS (
            SELECT 1
            FROM audit_events AS a
            WHERE a.action = 'tool.disable'
              AND a.target = t.id::text
          )
        """
    )


def downgrade() -> None:
    # Irreversible data fix — cannot distinguish prior intentional disables.
    pass
