"""Compatibilidade real do recorder com PostgreSQL, executada pelo serviço do CI."""

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from indusguard_api.agent import (
    AgentDecision,
    AgentIntentDecision,
    AgentObservability,
    AgentObservabilityStatus,
    AgentRunMetrics,
    AgentRunRequest,
    AgentRunResult,
    AgentRunStatus,
    AgentTerminationReason,
    ObservabilityComponentStatus,
)
from indusguard_api.persistence import SqlAlchemyAgentRunRecorder


@pytest.mark.postgres
def test_postgres_round_trip_uses_same_contract_as_sqlite() -> None:
    database_url = os.getenv("INDUSGUARD_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("INDUSGUARD_TEST_POSTGRES_URL não configurada")

    async def scenario() -> tuple[AgentRunResult, object]:
        recorder = SqlAlchemyAgentRunRecorder.from_url(database_url)
        now = datetime.now(UTC)
        result = AgentRunResult(
            run_id=str(uuid4()),
            started_at=now,
            completed_at=now,
            connector_id="synthetic",
            status=AgentRunStatus.COMPLETED,
            intent=AgentIntentDecision(intent_id="consultar"),
            decision=AgentDecision.ORIENT,
            answer="Consulta concluída.",
            evidence_ids=[],
            evidence=[],
            uncertainties=[],
            tool_calls=[],
            metrics=AgentRunMetrics(
                model="scripted-test-model",
                prompt_version="agent-v1",
                domain_version="0" * 64,
                policy_version="policy-v1",
                model_calls=2,
                tool_calls=0,
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                latency_ms=1,
                termination_reason=AgentTerminationReason.COMPLETED,
                truncations=0,
            ),
            observability=AgentObservability(
                status=AgentObservabilityStatus.HEALTHY,
                persistence=ObservabilityComponentStatus.RECORDED,
            ),
        )
        request = AgentRunRequest(connector_id="synthetic", message="Consulte.")
        await recorder.record(request=request, result=result)
        stored = await recorder.get(result.run_id)
        await recorder.dispose()
        return result, stored

    result, stored = asyncio.run(scenario())
    assert stored is not None
    assert stored.result.run_id == result.run_id
    assert stored.request_message == "Consulte."
