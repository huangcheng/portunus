"""audit index + tool_calls table for Fleet/Logs

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-11 20:50:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("key_id", sa.String(64), nullable=False),
        sa.Column("server_slug", sa.String(100), nullable=False),
        sa.Column("tool", sa.String(200), nullable=False),
        sa.Column("outcome", sa.String(64), nullable=False),
        sa.Column("site_id", sa.String(100), nullable=True),
        sa.Column("cold_start", sa.Boolean(), nullable=True),
        sa.Column("preferred_miss", sa.Boolean(), nullable=True),
        sa.Column("gateway_ms", sa.Integer(), nullable=True),
        sa.Column("start_ms", sa.Integer(), nullable=True),
        sa.Column("queue_ms", sa.Integer(), nullable=True),
        sa.Column("init_ms", sa.Integer(), nullable=True),
        sa.Column("upstream_ms", sa.Integer(), nullable=True),
        sa.Column("result_bytes", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_calls")),
    )
    op.create_index("ix_tool_calls_created_at", "tool_calls", ["created_at"])
    op.create_index("ix_tool_calls_trace_id", "tool_calls", ["trace_id"])


def downgrade() -> None:
    op.drop_index("ix_tool_calls_trace_id", table_name="tool_calls")
    op.drop_index("ix_tool_calls_created_at", table_name="tool_calls")
    op.drop_table("tool_calls")
    op.drop_index("ix_audit_events_created_at", table_name="audit_events")
