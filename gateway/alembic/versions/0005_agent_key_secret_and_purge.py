"""agent_keys recoverable secret + purge soft-revoked rows

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-10 15:10:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("agent_keys", schema=None) as batch_op:
        batch_op.add_column(sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=True))
        batch_op.add_column(sa.Column("secret_key_version", sa.Integer(), nullable=True))
    # Soft-revoked keys are no longer listed; grants cascade via FK.
    op.execute(sa.text("DELETE FROM agent_keys WHERE revoked_at IS NOT NULL"))


def downgrade() -> None:
    with op.batch_alter_table("agent_keys", schema=None) as batch_op:
        batch_op.drop_column("secret_key_version")
        batch_op.drop_column("secret_ciphertext")
