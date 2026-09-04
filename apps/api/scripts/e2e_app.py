"""Composition root offline usada somente pelo Playwright.

O arquivo fica fora da wheel de produção e injeta o fake pelo seam explícito da app factory. A
cadeia restante é real: FastAPI, PublicRunHost, LangGraph, MCP, policy, executor e ASGI synthetic.
"""

import os
from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage, ToolMessage

from indusguard_api.agent import (
    AgentDecision,
    AgentFinalAnswer,
    AgentIntentDecision,
    AgentModelGateway,
    AgentPlannedToolCall,
    AgentPlanStep,
    GatewayResult,
    TokenUsage,
)
from indusguard_api.main import create_app
from indusguard_api.settings import Settings

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
E2E_OWNER_TOKEN = "e2e-owner-token-with-at-least-thirty-two-chars"


class RepeatingE2EGateway(AgentModelGateway):
    @property
    def model_name(self) -> str:
        return "scripted-e2e-model"

    async def classify(self, **_: Any) -> GatewayResult[AgentIntentDecision]:
        return GatewayResult(AgentIntentDecision(intent_id="consultar"), TokenUsage())

    async def plan(self, *, messages: list[BaseMessage], **_: Any) -> GatewayResult[AgentPlanStep]:
        if any(isinstance(message, ToolMessage) for message in messages):
            return GatewayResult(AgentPlanStep(done=True), TokenUsage())
        return GatewayResult(
            AgentPlanStep(
                tool_calls=[
                    AgentPlannedToolCall(
                        alias="synthetic__getWidget",
                        arguments={"path": {"widgetId": "widget-1"}},
                    )
                ]
            ),
            TokenUsage(),
        )

    async def finalize(
        self,
        *,
        allowed_evidence_ids: list[str],
        **_: Any,
    ) -> GatewayResult[AgentFinalAnswer]:
        cited = list(allowed_evidence_ids[:1])
        suffix = f" [{cited[0]}]" if cited else ""
        return GatewayResult(
            AgentFinalAnswer(
                answer=f"O widget está ativo{suffix}.",
                decision=AgentDecision.ORIENT,
                evidence_ids=cited,
            ),
            TokenUsage(),
        )


gateway = RepeatingE2EGateway()

settings = Settings(
    _env_file=None,
    environment="e2e",
    execution_mode="simulate",
    connectors_dir=REPOSITORY_ROOT / "connectors",
    cors_allowed_origins=["http://127.0.0.1:3100"],
    database_url=os.environ.get(
        "INDUSGUARD_DATABASE_URL",
        "sqlite+aiosqlite:///./.data/e2e-dashboard.db",
    ),
    persist_runs=True,
    trace_jsonl_enabled=False,
    otlp_enabled=False,
    public_runs_enabled=True,
    public_connector_ids=["synthetic"],
    owner_token=E2E_OWNER_TOKEN,
)

app = create_app(settings=settings, public_model_gateway=gateway)
