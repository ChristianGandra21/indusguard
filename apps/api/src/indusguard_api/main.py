"""Aplicação HTTP que expõe o estado atual do núcleo IndusGuard.

As rotas desta etapa permitem verificar se o processo está vivo, se os conectores são válidos e
quais operações foram descobertas. O endpoint de execução e o agente serão adicionados somente
depois que essa camada declarativa estiver estável e testada.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status

from indusguard_api import __version__
from indusguard_api.connectors import ConnectorCatalog
from indusguard_api.schemas import (
    ConnectorSummary,
    HealthResponse,
    OperationSummary,
    ReadyResponse,
    VersionResponse,
)
from indusguard_api.settings import Settings


def create_app(*, connectors_dir: Path | None = None) -> FastAPI:
    """Cria uma instância isolada da aplicação.

    Usar uma factory, em vez de configurar tudo diretamente na variável global, permite que os
    testes apontem para diretórios temporários sem alterar variáveis globais ou o ambiente real.
    """

    settings = Settings(connectors_dir=connectors_dir) if connectors_dir else Settings()
    catalog = ConnectorCatalog(settings.connectors_dir)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Carregar no startup implementa o comportamento fail-fast: um conector inválido impede
        # que o serviço anuncie readiness com um catálogo incompleto.
        catalog.load()
        yield

    application = FastAPI(
        title="IndusGuard API",
        summary="Runtime genérico para agentes conectados a APIs OpenAPI",
        version=__version__,
        lifespan=lifespan,
    )
    # ``app.state`` disponibiliza dependências já construídas às rotas sem recriá-las a cada
    # request. Em uma etapa futura isso poderá migrar para dependency injection explícita.
    application.state.settings = settings
    application.state.connector_catalog = catalog

    @application.get(
        f"{settings.api_prefix}/health",
        response_model=HealthResponse,
        tags=["system"],
    )
    async def health() -> HealthResponse:
        """Liveness: confirma apenas que o processo HTTP consegue responder."""

        return HealthResponse()

    @application.get(
        f"{settings.api_prefix}/ready",
        response_model=ReadyResponse,
        tags=["system"],
    )
    async def ready(request: Request) -> ReadyResponse:
        """Readiness: confirma que o startup terminou e informa quantos conectores carregaram."""

        return ReadyResponse(connector_count=len(request.app.state.connector_catalog.list()))

    @application.get(
        f"{settings.api_prefix}/version",
        response_model=VersionResponse,
        tags=["system"],
    )
    async def version(request: Request) -> VersionResponse:
        """Expõe versão e modo de execução para diagnóstico e rastreabilidade de releases."""

        current_settings: Settings = request.app.state.settings
        return VersionResponse(
            version=__version__,
            environment=current_settings.environment,
            execution_mode=current_settings.execution_mode,
        )

    @application.get(
        f"{settings.api_prefix}/connectors",
        response_model=list[ConnectorSummary],
        tags=["connectors"],
    )
    async def list_connectors(request: Request) -> list[ConnectorSummary]:
        """Lista integrações disponíveis sem revelar credenciais ou URLs resolvidas."""

        return request.app.state.connector_catalog.list()

    @application.get(
        f"{settings.api_prefix}/connectors/{{connector_id}}/operations",
        response_model=list[OperationSummary],
        tags=["connectors"],
    )
    async def list_operations(connector_id: str, request: Request) -> list[OperationSummary]:
        """Mostra as operações e políticas públicas de um conector específico."""

        connector = request.app.state.connector_catalog.get(connector_id)
        if connector is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"conector '{connector_id}' não encontrado",
            )
        return connector.operations

    return application


# O Uvicorn importa ``indusguard_api.main:app``. Manter a factory acima preserva testabilidade,
# enquanto esta instância oferece o ponto de entrada convencional para execução local.
app = create_app()
