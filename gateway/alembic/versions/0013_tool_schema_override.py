"""tools.schema_override for operator-pinned JSON Schema

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-13 22:40:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tools", sa.Column("schema_override", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("tools", "schema_override")
