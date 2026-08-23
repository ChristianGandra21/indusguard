"""Cria checkpoints e resultados do benchmark eval-driven.

Revision ID: 20260823_0002
Revises: 20260823_0001
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260823_0002"
down_revision: str | None = "20260823_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evaluation_runs",
        sa.Column("evaluation_id", sa.String(length=36), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("dataset_version", sa.String(length=128), nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.Column("golden_digest", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("git_commit", sa.String(length=64), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("evaluation_id"),
    )
    op.create_index("ix_evaluation_runs_phase", "evaluation_runs", ["phase"])
    op.create_index("ix_evaluation_runs_status", "evaluation_runs", ["status"])
    op.create_index("ix_evaluation_runs_dataset_version", "evaluation_runs", ["dataset_version"])
    op.create_table(
        "evaluation_results",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("evaluation_id", sa.String(length=36), nullable=False),
        sa.Column("case_id", sa.String(length=128), nullable=False),
        sa.Column("scenario_id", sa.String(length=32), nullable=False),
        sa.Column("variant", sa.String(length=32), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("result_status", sa.String(length=32), nullable=False),
        sa.Column("termination_reason", sa.String(length=64), nullable=False),
        sa.Column("agent_run_id", sa.String(length=36), nullable=False),
        sa.Column("observations", sa.JSON(), nullable=False),
        sa.Column("score", sa.JSON(), nullable=True),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.run_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["evaluation_id"], ["evaluation_runs.evaluation_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "evaluation_id", "case_id", "variant", "seed", name="uq_evaluation_result_identity"
        ),
    )
    for column in (
        "evaluation_id",
        "case_id",
        "scenario_id",
        "variant",
        "result_status",
        "termination_reason",
        "agent_run_id",
    ):
        op.create_index(f"ix_evaluation_results_{column}", "evaluation_results", [column])


def downgrade() -> None:
    for column in (
        "agent_run_id",
        "termination_reason",
        "result_status",
        "variant",
        "scenario_id",
        "case_id",
        "evaluation_id",
    ):
        op.drop_index(f"ix_evaluation_results_{column}", table_name="evaluation_results")
    op.drop_table("evaluation_results")
    op.drop_index("ix_evaluation_runs_dataset_version", table_name="evaluation_runs")
    op.drop_index("ix_evaluation_runs_status", table_name="evaluation_runs")
    op.drop_index("ix_evaluation_runs_phase", table_name="evaluation_runs")
    op.drop_table("evaluation_runs")
