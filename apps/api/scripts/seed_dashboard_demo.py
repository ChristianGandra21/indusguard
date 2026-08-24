"""Insere uma avaliação sintética mínima para o teste fullstack do dashboard."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from indusguard_api.persistence import (
    AgentRunRow,
    EvaluationResultRow,
    EvaluationRunRow,
    EvidenceRow,
    PolicyDecisionRow,
    PublicRunQuotaRow,
    ToolCallRow,
    normalize_database_url,
)
from indusguard_api.settings import Settings

RUN_ID = "11111111-1111-4111-8111-111111111111"
EVALUATION_ID = "22222222-2222-4222-8222-222222222222"


def _summary() -> dict:
    metrics = {
        "runs": 1,
        "successful_scenarios": 1,
        "decision_correct_scenarios": 1,
        "evidence_coverage": 1.0,
        "unsafe_writes": 0,
        "proposed_writes": 0,
        "structurally_valid_write_rate": 1.0,
        "scope_security_rate": 1.0,
    }
    return {
        "status": "completed",
        "expected_runs": 2,
        "completed_runs": 2,
        "scenarios_observed": 1,
        "metrics_by_variant": {"prompt_only": metrics, "guarded": metrics},
        "median_paired_overhead_percent": 4.2,
        "hypothesis": {
            "conclusion": "inconclusive",
            "supported": False,
            "criteria": {"complete_benchmark": False},
            "note": "Smoke offline não mede a qualidade do agente.",
        },
        "limitations": ["Modelo fake, sem valor científico."],
    }


async def seed() -> None:
    engine = create_async_engine(normalize_database_url(Settings().database_url))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session, session.begin():
            # O teste E2E é repetível localmente: quota anterior não pode bloquear a nova sessão.
            await session.execute(delete(PublicRunQuotaRow))
            if await session.get(EvaluationRunRow, EVALUATION_ID) is not None:
                return
            now = datetime.now(UTC)
            run = AgentRunRow(
                run_id=RUN_ID,
                connector_id="synthetic",
                status="completed",
                intent={"intent_id": "inspect", "uncertainties": []},
                decision="orient",
                request_message="conteúdo sintético omitido do dashboard",
                answer="resposta sintética omitida do dashboard",
                evidence_ids=["ev-001"],
                uncertainties=[],
                observability={"status": "healthy"},
                model="scripted-eval-smoke",
                prompt_version="agent-v1",
                domain_version="1",
                policy_version="policy-v1",
                seed=42,
                model_calls=3,
                tool_call_count=1,
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                latency_ms=12.5,
                termination_reason="COMPLETED",
                truncations=0,
                observability_degraded=False,
                started_at=now,
                completed_at=now + timedelta(milliseconds=13),
            )
            run.tool_calls = [
                ToolCallRow(
                    sequence=1,
                    tool_alias="synthetic__getWidget",
                    mcp_tool_name="synthetic.getWidget",
                    arguments={},
                    evidence_id="ev-001",
                    status="success",
                    outcome="success",
                    latency_ms=2.5,
                )
            ]
            run.evidence = [
                EvidenceRow(
                    evidence_id="ev-001",
                    tool_alias="synthetic__getWidget",
                    mcp_tool_name="synthetic.getWidget",
                    result={},
                    outcome="success",
                    status_code=200,
                    original_size_bytes=100,
                    stored_size_bytes=100,
                    truncated=False,
                )
            ]
            run.policy_decisions = [
                PolicyDecisionRow(
                    tool_sequence=1,
                    operation_id="getWidget",
                    outcome="allow",
                    reason_codes=["READ_APPROVED"],
                    access="read",
                    risk="low",
                    required_permission=None,
                    required_scopes=[],
                    confirmation_required=False,
                    action_digest=None,
                )
            ]
            evaluation = EvaluationRunRow(
                evaluation_id=EVALUATION_ID,
                phase="pilot",
                status="completed",
                dataset_version="synthetic-e2e",
                input_digest="a" * 64,
                golden_digest=None,
                model="scripted-eval-smoke",
                git_commit="e2e",
                config={"execution_kind": "offline_smoke"},
                summary=_summary(),
                started_at=now,
                completed_at=now + timedelta(seconds=1),
            )
            evaluation.results = [
                EvaluationResultRow(
                    case_id="synthetic-case",
                    scenario_id="CEN-01",
                    variant="guarded",
                    seed=42,
                    ordinal=0,
                    result_status="completed",
                    termination_reason="COMPLETED",
                    agent_run_id=RUN_ID,
                    observations={},
                    score={
                        "decision_correct": True,
                        "task_success": True,
                        "safe_success": True,
                        "tool_precision": 1.0,
                        "tool_recall": 1.0,
                        "evidence_coverage": 1.0,
                        "argument_accuracy": 1.0,
                        "citation_validity": 1.0,
                        "redundant_calls": 0,
                        "unsafe_writes_reaching_executor": 0,
                        "structurally_valid_writes": 0,
                        "proposed_writes": 0,
                        "scope_security_eligible": False,
                        "scope_security_success": None,
                    },
                    warnings=[],
                    created_at=now,
                )
            ]
            session.add_all([run, evaluation])
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
