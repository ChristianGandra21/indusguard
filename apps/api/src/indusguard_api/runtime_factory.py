"""Composition root interno para montar todas as camadas com a mesma observabilidade.

FastAPI ainda não chama esta factory. Ela existe para avaliações, scripts manuais e a futura rota
não precisarem repetir — ou acidentalmente divergir — a configuração de executor, policy, banco e
telemetria.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from indusguard_api.agent import AgentModelGateway, AgentRuntime, AgentRuntimeConfig
from indusguard_api.connectors import ConnectorCatalog
from indusguard_api.executor import HttpExecutor
from indusguard_api.observability import Telemetry, telemetry_from_settings
from indusguard_api.persistence import SqlAlchemyAgentRunRecorder
from indusguard_api.policy import GuardedExecutor, PolicyEngine
from indusguard_api.settings import Settings


@dataclass
class InternalAgentHost:
    """Recursos com ciclo de vida explícito, sem registrar rota ou abrir porta."""

    runtime: AgentRuntime
    telemetry: Telemetry
    recorder: SqlAlchemyAgentRunRecorder | None
    owns_telemetry: bool = True
    owns_recorder: bool = True

    async def close(self) -> None:
        """Entrega spans pendentes e encerra pools sem ocultar a resposta de uma run."""

        if self.owns_telemetry:
            self.telemetry.force_flush()
            self.telemetry.shutdown()
        if self.recorder is not None and self.owns_recorder:
            await self.recorder.dispose()


def create_internal_agent_host(
    *,
    catalog: ConnectorCatalog,
    model_gateway: AgentModelGateway,
    settings: Settings,
    http_client: httpx.AsyncClient | None = None,
    environment: Mapping[str, str] | None = None,
    runtime_config: AgentRuntimeConfig | None = None,
    telemetry: Telemetry | None = None,
    recorder: SqlAlchemyAgentRunRecorder | None = None,
) -> InternalAgentHost:
    """Monta o caminho obrigatório LangGraph → MCP → policy → executor observado."""

    owns_telemetry = telemetry is None
    current_telemetry = telemetry or telemetry_from_settings(settings)
    owns_recorder = recorder is None
    current_recorder = recorder
    if current_recorder is None and settings.persist_runs:
        current_recorder = SqlAlchemyAgentRunRecorder.from_url(settings.database_url)
    http_executor = HttpExecutor(
        catalog,
        environment=environment,
        client=http_client,
        execution_mode=settings.execution_mode,
        telemetry=current_telemetry,
    )
    policy_engine = PolicyEngine(
        catalog,
        execution_mode=settings.execution_mode,
        telemetry=current_telemetry,
    )
    guarded_executor = GuardedExecutor(
        policy_engine,
        http_executor,
        telemetry=current_telemetry,
    )
    runtime = AgentRuntime(
        catalog,
        guarded_executor,
        model_gateway,
        runtime_config,
        recorder=current_recorder,
        telemetry=current_telemetry,
    )
    return InternalAgentHost(
        runtime=runtime,
        telemetry=current_telemetry,
        recorder=current_recorder,
        owns_telemetry=owns_telemetry,
        owns_recorder=owns_recorder,
    )
