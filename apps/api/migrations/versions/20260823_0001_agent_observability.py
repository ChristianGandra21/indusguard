"""Cria persistência auditável de runs, tools, evidências e decisões políticas.

Revision ID: 20260823_0001
Revises:
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260823_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("connector_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("intent", sa.JSON(), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("request_message", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("uncertainties", sa.JSON(), nullable=False),
        sa.Column("observability", sa.JSON(), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("prompt_version", sa.String(length=128), nullable=False),
        sa.Column("domain_version", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=128), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("model_calls", sa.Integer(), nullable=False),
        sa.Column("tool_call_count", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("termination_reason", sa.String(length=64), nullable=False),
        sa.Column("truncations", sa.Integer(), nullable=False),
        sa.Column("observability_degraded", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("ix_agent_runs_connector_id", "agent_runs", ["connector_id"])
    op.create_index("ix_agent_runs_status", "agent_runs", ["status"])
    op.create_index("ix_agent_runs_decision", "agent_runs", ["decision"])
    op.create_index(
        "ix_agent_runs_termination_reason",
        "agent_runs",
        ["termination_reason"],
    )

    op.create_table(
        "tool_calls",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("tool_alias", sa.String(length=128), nullable=False),
        sa.Column("mcp_tool_name", sa.String(length=128), nullable=True),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("evidence_id", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=128), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tool_calls_run_id", "tool_calls", ["run_id"])
    op.create_index("ix_tool_calls_outcome", "tool_calls", ["outcome"])
    op.create_index(
        "ix_tool_calls_run_sequence",
        "tool_calls",
        ["run_id", "sequence"],
        unique=True,
    )

    op.create_table(
        "agent_evidence",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("evidence_id", sa.String(length=32), nullable=False),
        sa.Column("tool_alias", sa.String(length=128), nullable=False),
        sa.Column("mcp_tool_name", sa.String(length=128), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("outcome", sa.String(length=128), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("original_size_bytes", sa.Integer(), nullable=False),
        sa.Column("stored_size_bytes", sa.Integer(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_evidence_run_id", "agent_evidence", ["run_id"])
    op.create_index("ix_agent_evidence_outcome", "agent_evidence", ["outcome"])
    op.create_index(
        "ix_agent_evidence_run_evidence",
        "agent_evidence",
        ["run_id", "evidence_id"],
        unique=True,
    )

    op.create_table(
        "policy_decisions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("tool_sequence", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("outcome", sa.String(length=64), nullable=False),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("access", sa.String(length=32), nullable=True),
        sa.Column("risk", sa.String(length=32), nullable=True),
        sa.Column("required_permission", sa.String(length=128), nullable=True),
        sa.Column("required_scopes", sa.JSON(), nullable=False),
        sa.Column("confirmation_required", sa.Boolean(), nullable=False),
        sa.Column("action_digest", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_policy_decisions_run_id", "policy_decisions", ["run_id"])
    op.create_index("ix_policy_decisions_outcome", "policy_decisions", ["outcome"])
    op.create_index(
        "ix_policy_decisions_run_sequence",
        "policy_decisions",
        ["run_id", "tool_sequence"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_policy_decisions_run_sequence", table_name="policy_decisions")
    op.drop_index("ix_policy_decisions_outcome", table_name="policy_decisions")
    op.drop_index("ix_policy_decisions_run_id", table_name="policy_decisions")
    op.drop_table("policy_decisions")
    op.drop_index("ix_agent_evidence_run_evidence", table_name="agent_evidence")
    op.drop_index("ix_agent_evidence_outcome", table_name="agent_evidence")
    op.drop_index("ix_agent_evidence_run_id", table_name="agent_evidence")
    op.drop_table("agent_evidence")
    op.drop_index("ix_tool_calls_run_sequence", table_name="tool_calls")
    op.drop_index("ix_tool_calls_outcome", table_name="tool_calls")
    op.drop_index("ix_tool_calls_run_id", table_name="tool_calls")
    op.drop_table("tool_calls")
    op.drop_index("ix_agent_runs_termination_reason", table_name="agent_runs")
    op.drop_index("ix_agent_runs_decision", table_name="agent_runs")
    op.drop_index("ix_agent_runs_status", table_name="agent_runs")
    op.drop_index("ix_agent_runs_connector_id", table_name="agent_runs")
    op.drop_table("agent_runs")
