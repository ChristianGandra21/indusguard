"""Factories pequenas para manter os testes de avaliação focados no comportamento."""

from datetime import UTC, datetime

from indusguard_api.agent import (
    AgentDecision,
    AgentEvidence,
    AgentIntentDecision,
    AgentObservability,
    AgentRunMetrics,
    AgentRunResult,
    AgentRunStatus,
    AgentTerminationReason,
    AgentToolCall,
)


def agent_result(
    *,
    decision: AgentDecision = AgentDecision.ORIENT,
    tool_calls: list[AgentToolCall] | None = None,
    evidence: list[AgentEvidence] | None = None,
    evidence_ids: list[str] | None = None,
    termination: AgentTerminationReason = AgentTerminationReason.COMPLETED,
    latency_ms: float = 100,
) -> AgentRunResult:
    """Cria o menor resultado válido sem esconder defaults relevantes para o scorer."""

    now = datetime.now(UTC)
    calls = tool_calls or []
    collected = evidence or []
    cited = evidence_ids if evidence_ids is not None else [item.id for item in collected]
    return AgentRunResult(
        run_id="00000000-0000-0000-0000-000000000001",
        started_at=now,
        completed_at=now,
        connector_id="tractian",
        status=(
            AgentRunStatus.COMPLETED
            if termination is AgentTerminationReason.COMPLETED
            else AgentRunStatus.PARTIAL
        ),
        intent=AgentIntentDecision(intent_id="investigate_asset"),
        decision=decision,
        answer="Resposta fundamentada nas evidências coletadas.",
        evidence_ids=cited,
        evidence=collected,
        uncertainties=[],
        tool_calls=calls,
        metrics=AgentRunMetrics(
            model="fake-eval-model",
            prompt_version="agent-v1",
            domain_version="official-v1",
            policy_version="policy-v1",
            model_calls=3,
            tool_calls=len(calls),
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            latency_ms=latency_ms,
            termination_reason=termination,
            truncations=0,
        ),
        observability=AgentObservability(),
    )


def tool_call(
    operation_id: str,
    *,
    evidence_id: str,
    arguments: dict[str, object] | None = None,
    status: str = "completed",
    outcome: str = "success",
) -> AgentToolCall:
    return AgentToolCall(
        tool_alias=f"tractian__{operation_id}",
        mcp_tool_name=f"tractian.{operation_id}",
        arguments=arguments or {},
        evidence_id=evidence_id,
        status=status,
        outcome=outcome,
        latency_ms=1,
    )


def evidence(
    operation_id: str,
    *,
    evidence_id: str,
    outcome: str = "success",
) -> AgentEvidence:
    return AgentEvidence(
        id=evidence_id,
        tool_alias=f"tractian__{operation_id}",
        mcp_tool_name=f"tractian.{operation_id}",
        result={"execution": {"outcome": outcome}},
        outcome=outcome,
        status_code=200,
        original_size_bytes=10,
        stored_size_bytes=10,
    )
