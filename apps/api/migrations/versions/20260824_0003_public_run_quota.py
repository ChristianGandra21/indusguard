"""Cria a janela persistente de quota do playground.

Revision ID: 20260824_0003
Revises: 20260823_0002
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260824_0003"
down_revision: str | None = "20260823_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "public_run_quota",
        sa.Column("subject", sa.String(length=128), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_runs", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("subject"),
    )


def downgrade() -> None:
    op.drop_table("public_run_quota")
