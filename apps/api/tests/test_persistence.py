"""Aceitação de persistência e observabilidade no fluxo LangGraph completo."""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import REPOSITORY_ROOT

from indusguard_api.agent import (
    AgentDecision,
    AgentFinalAnswer,
    AgentIntentDecision,
    AgentPlannedToolCall,
    AgentPlanStep,
    AgentRunRequest,
    AgentRunResult,
    AgentRuntime,
    ScriptedAgentModelGateway,
    TrustedRunContext,
)
from indusguard_api.connectors import ConnectorCatalog
from indusguard_api.executor import HttpExecutor
from indusguard_api.observability import NoOpTelemetry, OpenTelemetryRuntime, Telemetry
from indusguard_api.persistence import PolicyDecisionRow, SqlAlchemyAgentRunRecorder
from indusguard_api.policy import GuardedExecutor, PolicyEngine
from indusguard_api.schemas import PolicyPrincipal


class FailingRecorder:
    """Simula indisponibilidade sem carregar uma exceção técnica na resposta."""

    async def record(self, **_: Any) -> None:
        raise RuntimeError("postgresql://user:secret@database.internal")


def _gateway(*, answer: str = "A alteração foi simulada [ev-001].") -> ScriptedAgentModelGateway:
    return ScriptedAgentModelGateway(
        classification=AgentIntentDecision(intent_id="atualizar"),
        plans=[
            AgentPlanStep(
                tool_calls=[
                    AgentPlannedToolCall(
                        alias="synthetic__updateWidget",
                        arguments={
                            "path": {"widgetId": "widget-1"},
                            "body": {
                                "status": "inactive",
                                "justification": (
                                    "manutenção preventiva autorizada; Bearer justificativa-secreta"
                                ),
                            },
                        },
                    )
                ]
            ),
            AgentPlanStep(done=True),
        ],
        final_answer=AgentFinalAnswer(
            answer=answer,
            decision=AgentDecision.ACT,
            evidence_ids=["ev-001"],
        ),
    )


async def _runtime(
    *,
    gateway: ScriptedAgentModelGateway,
    recorder: Any,
    telemetry: Telemetry,
) -> AgentRuntime:
    catalog = ConnectorCatalog(REPOSITORY_ROOT / "connectors")
    catalog.load()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(500, json={"message": "não deveria acessar a rede"})
        )
    )
    policy = PolicyEngine(catalog, telemetry=telemetry)
    executor = HttpExecutor(catalog, client=client, telemetry=telemetry)
    guarded = GuardedExecutor(policy, executor, telemetry=telemetry)
    runtime = AgentRuntime(
        catalog,
        guarded,
        gateway,
        recorder=recorder,
        telemetry=telemetry,
    )
    # O cliente não será usado porque a escrita é simulada; fechá-lo evita recursos pendentes.
    await client.aclose()
    return runtime


def _request() -> AgentRunRequest:
    return AgentRunRequest(
        connector_id="synthetic",
        message="Desative o widget; token=segredo-do-usuario",
        seed=17,
    )


def _trusted_context() -> TrustedRunContext:
    return TrustedRunContext(
        principal=PolicyPrincipal(id="user-1", permissions=["action_high"]),
        direct_request=True,
    )


def test_persists_complete_run_and_reconstructs_redacted_history(tmp_path: Path) -> None:
    async def scenario() -> tuple[AgentRunResult, Any]:
        recorder = SqlAlchemyAgentRunRecorder.from_url(
            f"sqlite+aiosqlite:///{tmp_path / 'runs.db'}"
        )
        await recorder.create_schema_for_tests()
        telemetry = OpenTelemetryRuntime(
            service_name="indusguard-test",
            jsonl_path=tmp_path / "traces.jsonl",
        )
        runtime = await _runtime(gateway=_gateway(), recorder=recorder, telemetry=telemetry)
        result = await runtime.run(_request(), _trusted_context())
        stored = await recorder.get(result.run_id)
        telemetry.shutdown()
        await recorder.dispose()
        return result, stored

    result, stored = asyncio.run(scenario())

    assert result.observability.status == "healthy"
    assert result.observability.persistence == "recorded"
    assert result.metrics.observability_degraded is False
    assert stored is not None
    assert stored.result.run_id == result.run_id
    assert stored.result.started_at == result.started_at.replace(tzinfo=None)
    assert stored.request_message == "Desative o widget; token=[REDACTED]"
    assert (
        stored.result.tool_calls[0].arguments["body"]["justification"].endswith("Bearer [REDACTED]")
    )
    assert stored.result.evidence[0].id == "ev-001"
    assert stored.policy_decisions[0]["outcome"] == "simulate"
    assert stored.policy_decisions[0]["operation_id"] == "updateWidget"

    trace_lines = (tmp_path / "traces.jsonl").read_text(encoding="utf-8").splitlines()
    traces = [json.loads(line) for line in trace_lines]
    names = {trace["name"] for trace in traces}
    assert {
        "indusguard.agent.run",
        "indusguard.model.classify",
        "indusguard.model.plan",
        "indusguard.model.finalize",
        "indusguard.tool.call",
        "indusguard.action",
        "indusguard.policy.evaluate",
        "indusguard.http.execute",
        "indusguard.persistence.save",
    } <= names
    assert {trace["trace_id"] for trace in traces} == {traces[0]["trace_id"]}
    serialized = json.dumps(traces, ensure_ascii=False)
    assert "segredo-do-usuario" not in serialized
    assert "justificativa-secreta" not in serialized


def test_persistence_failure_returns_answer_with_prominent_warning() -> None:
    async def scenario() -> AgentRunResult:
        telemetry = NoOpTelemetry()
        runtime = await _runtime(
            gateway=_gateway(),
            recorder=FailingRecorder(),
            telemetry=telemetry,
        )
        return await runtime.run(_request(), _trusted_context())

    result = asyncio.run(scenario())

    assert result.status == "completed"
    assert result.answer == "A alteração foi simulada [ev-001]."
    assert result.observability.status == "degraded"
    assert result.observability.warning_code == "OBSERVABILITY_DEGRADED"
    assert result.observability.persistence == "failed"
    assert result.metrics.observability_degraded is True
    assert "OBSERVABILITY_DEGRADED" in result.uncertainties
    assert "secret" not in result.model_dump_json()


def test_local_trace_failure_is_visible_but_does_not_fail_run(tmp_path: Path) -> None:
    async def scenario() -> AgentRunResult:
        blocked = tmp_path / "blocked"
        blocked.write_text("file", encoding="utf-8")
        telemetry = OpenTelemetryRuntime(
            service_name="indusguard-test",
            jsonl_path=blocked / "traces.jsonl",
        )
        runtime = await _runtime(gateway=_gateway(), recorder=None, telemetry=telemetry)
        result = await runtime.run(_request(), _trusted_context())
        telemetry.shutdown()
        return result

    result = asyncio.run(scenario())

    assert result.status == "completed"
    assert result.observability.status == "degraded"
    assert result.observability.local_trace == "failed"
    assert result.observability.persistence == "disabled"


def test_recorder_is_idempotent_for_same_run_id(tmp_path: Path) -> None:
    async def scenario() -> Any:
        recorder = SqlAlchemyAgentRunRecorder.from_url(
            f"sqlite+aiosqlite:///{tmp_path / 'idempotent.db'}"
        )
        await recorder.create_schema_for_tests()
        runtime = await _runtime(
            gateway=_gateway(),
            recorder=recorder,
            telemetry=NoOpTelemetry(),
        )
        request = _request()
        result = await runtime.run(request, _trusted_context())
        await recorder.record(request=request, result=result)
        stored = await recorder.get(result.run_id)
        await recorder.dispose()
        return stored

    stored = asyncio.run(scenario())
    assert stored is not None
    assert len(stored.result.tool_calls) == 1
    assert len(stored.policy_decisions) == 1


def test_child_failure_rolls_back_entire_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uma violação em tabela filha não pode deixar o registro pai pela metade."""

    def duplicate_policy_rows(_: AgentRunResult) -> list[PolicyDecisionRow]:
        return [
            PolicyDecisionRow(
                tool_sequence=1,
                operation_id="updateWidget",
                outcome="simulate",
                reason_codes=["WRITE_SIMULATION_APPROVED"],
                required_scopes=[],
                confirmation_required=True,
            ),
            PolicyDecisionRow(
                tool_sequence=1,
                operation_id="updateWidget",
                outcome="simulate",
                reason_codes=["WRITE_SIMULATION_APPROVED"],
                required_scopes=[],
                confirmation_required=True,
            ),
        ]

    monkeypatch.setattr(
        "indusguard_api.persistence._policy_rows",
        duplicate_policy_rows,
    )

    async def scenario() -> tuple[AgentRunResult, Any]:
        recorder = SqlAlchemyAgentRunRecorder.from_url(
            f"sqlite+aiosqlite:///{tmp_path / 'rollback.db'}"
        )
        await recorder.create_schema_for_tests()
        runtime = await _runtime(
            gateway=_gateway(),
            recorder=recorder,
            telemetry=NoOpTelemetry(),
        )
        result = await runtime.run(_request(), _trusted_context())
        stored = await recorder.get(result.run_id)
        await recorder.dispose()
        return result, stored

    result, stored = asyncio.run(scenario())
    assert result.observability.status == "degraded"
    assert result.observability.persistence == "failed"
    assert stored is None
