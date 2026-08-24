"""Composition root offline usada somente pelo Playwright.

O arquivo fica fora da wheel de produção e injeta o fake pelo seam explícito da app factory. A
cadeia restante é real: FastAPI, PublicRunHost, LangGraph, MCP, policy, executor e ASGI synthetic.
"""

import os
from pathlib import Path

from indusguard_api.agent import (
    AgentDecision,
    AgentFinalAnswer,
    AgentIntentDecision,
    AgentPlannedToolCall,
    AgentPlanStep,
    ScriptedAgentModelGateway,
)
from indusguard_api.main import create_app
from indusguard_api.settings import Settings

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
E2E_OWNER_TOKEN = "e2e-owner-token-with-at-least-thirty-two-chars"

gateway = ScriptedAgentModelGateway(
    classification=AgentIntentDecision(intent_id="consultar"),
    plans=[
        AgentPlanStep(
            tool_calls=[
                AgentPlannedToolCall(
                    alias="synthetic__getWidget",
                    arguments={"path": {"widgetId": "widget-1"}},
                )
            ]
        ),
        AgentPlanStep(done=True),
    ],
    final_answer=AgentFinalAnswer(
        answer="O widget está ativo [ev-001].",
        decision=AgentDecision.ORIENT,
        evidence_ids=["ev-001"],
    ),
    model_name="scripted-e2e-model",
)

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
