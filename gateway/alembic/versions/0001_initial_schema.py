"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-09 19:43:09.847143
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('agent_keys',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('key_id', sa.String(length=64), nullable=False),
    sa.Column('digest', sa.LargeBinary(length=32), nullable=False),
    sa.Column('label', sa.String(length=200), nullable=True),
    sa.Column('generation', sa.Integer(), server_default='0', nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agent_keys')),
    sa.UniqueConstraint('key_id', name=op.f('uq_agent_keys_key_id'))
    )
    op.create_table('audit_events',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('action', sa.String(length=100), nullable=False),
    sa.Column('target', sa.String(length=200), nullable=True),
    sa.Column('detail', sa.JSON(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_events'))
    )
    op.create_table('operator_sessions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('token_hash', sa.LargeBinary(length=32), nullable=False),
    sa.Column('csrf_token', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_operator_sessions')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_operator_sessions_token_hash'))
    )
    op.create_table('servers',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('slug', sa.String(length=100), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('command', sa.String(length=1024), nullable=True),
    sa.Column('args', sa.JSON(), nullable=True),
    sa.Column('url', sa.String(length=2048), nullable=True),
    sa.Column('vault_version', sa.Integer(), server_default='0', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("kind IN ('stdio', 'remote')", name=op.f('ck_servers_kind')),
    sa.CheckConstraint('slug = lower(slug)', name=op.f('ck_servers_slug_lower')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_servers')),
    sa.UniqueConstraint('slug', name=op.f('uq_servers_slug'))
    )
    op.create_table('grants',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('key_id', sa.String(length=64), nullable=False),
    sa.Column('server_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['key_id'], ['agent_keys.key_id'], name=op.f('fk_grants_key_id_agent_keys'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['server_id'], ['servers.id'], name=op.f('fk_grants_server_id_servers'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_grants')),
    sa.UniqueConstraint('key_id', 'server_id', name=op.f('uq_grants_key_id_server_id'))
    )
    op.create_table('tools',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('server_id', sa.Uuid(), nullable=False),
    sa.Column('upstream_name', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('schema', sa.JSON(), nullable=False),
    sa.Column('schema_hash', sa.String(length=128), nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['server_id'], ['servers.id'], name=op.f('fk_tools_server_id_servers'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tools')),
    sa.UniqueConstraint('server_id', 'upstream_name', name=op.f('uq_tools_server_id_upstream_name'))
    )
    op.create_table('vault_entries',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('server_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('ciphertext', sa.LargeBinary(), nullable=False),
    sa.Column('key_version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['server_id'], ['servers.id'], name=op.f('fk_vault_entries_server_id_servers'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_vault_entries')),
    sa.UniqueConstraint('server_id', 'name', name=op.f('uq_vault_entries_server_id_name'))
    )


def downgrade() -> None:
    op.drop_table('vault_entries')
    op.drop_table('tools')
    op.drop_table('grants')
    op.drop_table('servers')
    op.drop_table('operator_sessions')
    op.drop_table('audit_events')
    op.drop_table('agent_keys')
