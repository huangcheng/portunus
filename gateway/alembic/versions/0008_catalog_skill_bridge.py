"""catalog_apps: allow skill_bridge connection (DB-backed manifests)

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-11 11:20:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW = "connection IN ('oauth', 'http', 'webdav', 'stdio', 'skill_bridge')"
_OLD = "connection IN ('oauth', 'http', 'webdav', 'stdio')"


def _pg_drop_connection_checks(conn) -> None:
    rows = conn.execute(
        sa.text(
            """
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'catalog_apps'::regclass AND contype = 'c'
            """
        )
    )
    for (name,) in rows:
        if "connection" in name:
            conn.execute(sa.text(f'ALTER TABLE catalog_apps DROP CONSTRAINT "{name}"'))


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        _pg_drop_connection_checks(bind)
        bind.execute(
            sa.text(
                "ALTER TABLE catalog_apps ADD CONSTRAINT ck_catalog_apps_connection "
                f"CHECK ({_NEW})"
            )
        )
        return
    # SQLite: recreate via batch. The constraint is stored under the models' naming
    # convention ("ck_%(table_name)s_%(constraint_name)s" applied to the explicit name,
    # i.e. the doubled form) and alembic expands the passed name by exactly one
    # convention application — so dropping/creating the *single* name is what matches.
    # Dropping the doubled name here was the original failure: it expanded to the
    # tripled form, missed, and batch raises at context exit (uncatchable per-op).
    with op.batch_alter_table("catalog_apps", naming_convention=None) as batch:
        batch.drop_constraint("ck_catalog_apps_connection", type_="check")
        batch.create_check_constraint("ck_catalog_apps_connection", _NEW)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        _pg_drop_connection_checks(bind)
        bind.execute(
            sa.text(
                "ALTER TABLE catalog_apps ADD CONSTRAINT ck_catalog_apps_connection "
                f"CHECK ({_OLD})"
            )
        )
        return
    with op.batch_alter_table("catalog_apps", naming_convention=None) as batch:
        try:
            batch.drop_constraint("ck_catalog_apps_connection", type_="check")
        except Exception:
            pass
        batch.create_check_constraint("ck_catalog_apps_connection", _OLD)
