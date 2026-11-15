"""operator account: password, TOTP, passkeys, recovery codes, auth events

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-15 16:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "operator_account",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("totp_secret", sa.Text(), nullable=True),
        sa.Column("totp_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("totp_last_step", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_operator_account_singleton")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_operator_account")),
        sa.UniqueConstraint("username", name=op.f("uq_operator_account_username")),
    )
    op.create_table(
        "passkey_credentials",
        sa.Column("credential_id", sa.String(length=512), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("sign_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("transports", sa.Text(), nullable=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("credential_id", name=op.f("pk_passkey_credentials")),
    )
    op.create_table(
        "recovery_codes",
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("code_hash", name=op.f("pk_recovery_codes")),
    )
    op.create_table(
        "auth_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("detail", sa.String(length=500), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_events")),
    )
    op.create_index(op.f("ix_auth_events_ts"), "auth_events", ["ts"], unique=False)
    op.create_index("ix_auth_events_ip_ts", "auth_events", ["ip", "ts"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_auth_events_ip_ts", table_name="auth_events")
    op.drop_index(op.f("ix_auth_events_ts"), table_name="auth_events")
    op.drop_table("auth_events")
    op.drop_table("recovery_codes")
    op.drop_table("passkey_credentials")
    op.drop_table("operator_account")
