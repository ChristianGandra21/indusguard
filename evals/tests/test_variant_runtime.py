"""Seam principal: modelo fake → LangGraph → MCP real → fixture ASGI."""

import asyncio
from pathlib import Path

import httpx
from indusguard_api.agent import (
    AgentDecision,
    AgentFinalAnswer,
    AgentIntentDecision,
    AgentPlannedToolCall,
    AgentPlanStep,
    ScriptedAgentModelGateway,
)
from indusguard_api.connectors import ConnectorCatalog
from indusguard_api.executor import HttpExecutor
from indusguard_api.policy import GuardedExecutor, PolicyEngine

from indusguard_evals.baseline import PromptOnlyExecutor
from indusguard_evals.contracts import EvaluationPhase, EvaluationVariant
from indusguard_evals.corpus import OfficialCorpus
from indusguard_evals.execution import create_variant_runtime
from indusguard_evals.schedule import build_schedule
from indusguard_evals.tractian_fixture import store
from indusguard_evals.tractian_fixture.main import app

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CORPUS_ROOT = REPOSITORY_ROOT / "evals" / "corpus" / "official-v1"


class CountingAsgiTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._transport = httpx.ASGITransport(app=app)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self._transport.aclose()


def _gateway() -> ScriptedAgentModelGateway:
    return ScriptedAgentModelGateway(
        classification=AgentIntentDecision(intent_id="agir"),
        plans=[
            AgentPlanStep(
                tool_calls=[
                    AgentPlannedToolCall(
                        alias="tractian__listAnalyses",
                        arguments={"path": {"assetId": "asset_C710"}},
                    ),
                    AgentPlannedToolCall(
                        alias="tractian__getAnalysis",
                        arguments={"path": {"analysisId": "an_9902"}},
                    ),
                    AgentPlannedToolCall(
                        alias="tractian__getBaseline",
                        arguments={"path": {"assetId": "asset_C710"}},
                    ),
                    AgentPlannedToolCall(
                        alias="tractian__requestSpecialistAnalysis",
                        arguments={
                            "path": {"analysisId": "an_9902"},
                            "body": {
                                "justification": (
                                    "tendência e baseline revisadas nas evidências anteriores"
                                )
                            },
                        },
                    ),
                ]
            ),
            AgentPlanStep(done=True),
        ],
        final_answer=AgentFinalAnswer(
            answer="A solicitação ao especialista foi simulada [ev-004].",
            decision=AgentDecision.ACT,
            evidence_ids=["ev-001", "ev-002", "ev-003", "ev-004"],
        ),
    )


def test_variants_share_context_and_mcp_but_only_policy_gate_changes() -> None:
    async def exercise() -> tuple[object, object, list[str], object, object]:
        store.configure_data_dir(CORPUS_ROOT / "fixture" / "data")
        inputs = OfficialCorpus(CORPUS_ROOT).load_inputs()
        case = next(item for item in inputs.cases if item.case_id == "case_tkt_exe_13")
        scheduled = [
            item
            for item in build_schedule(inputs, EvaluationPhase.PILOT)
            if item.case_id == case.case_id and item.seed == 42
        ]
        catalog = ConnectorCatalog(REPOSITORY_ROOT / "connectors")
        catalog.load()
        transport = CountingAsgiTransport()
        guarded_gateway = _gateway()
        baseline_gateway = _gateway()
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost:8000",
        ) as client:
            guarded_http = HttpExecutor(
                catalog,
                client=client,
                execution_mode="simulate",
                environment={"TRACTIAN_API_URL": "http://localhost:8000"},
            )
            baseline_http = HttpExecutor(
                catalog,
                client=client,
                execution_mode="simulate",
                environment={"TRACTIAN_API_URL": "http://localhost:8000"},
            )
            shadow = PolicyEngine(catalog, execution_mode="simulate")
            guarded = create_variant_runtime(
                variant=EvaluationVariant.GUARDED,
                catalog=catalog,
                executor=GuardedExecutor(shadow, guarded_http),
                shadow_policy=shadow,
                model_gateway=guarded_gateway,
            )
            baseline = create_variant_runtime(
                variant=EvaluationVariant.PROMPT_ONLY,
                catalog=catalog,
                executor=PromptOnlyExecutor(catalog, baseline_http),
                shadow_policy=shadow,
                model_gateway=baseline_gateway,
            )
            by_variant = {item.variant: item for item in scheduled}
            guarded_sample = await guarded.run(by_variant[EvaluationVariant.GUARDED], case)
            baseline_sample = await baseline.run(by_variant[EvaluationVariant.PROMPT_ONLY], case)
        return (
            guarded_sample,
            baseline_sample,
            [request.method for request in transport.requests],
            guarded_gateway.seen_planning_contexts[0],
            baseline_gateway.seen_planning_contexts[0],
        )

    guarded, baseline, methods, guarded_context, baseline_context = asyncio.run(exercise())

    assert methods == ["GET", "GET", "GET", "GET", "GET", "GET"]
    assert guarded.result.evidence[-1].outcome == "simulated"
    assert baseline.result.evidence[-1].outcome == "simulated"
    assert guarded.shadow_policy[-1].outcome == "simulate"
    assert baseline.shadow_policy[-1].outcome == "simulate"
    assert guarded_context == baseline_context
    assert "action_low" in guarded_context.permissions
    assert guarded_context.context["asset_id"] == "asset_C710"
