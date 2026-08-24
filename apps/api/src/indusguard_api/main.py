"""Aplicação HTTP que expõe o estado atual do núcleo IndusGuard.

As rotas desta etapa permitem verificar se o processo está vivo, se os conectores são válidos e
quais operações foram descobertas. O runtime do agente existe como interface interna, mas não é
exposto por estas rotas: publicação exige autenticação, rate limit e observabilidade próprios.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import SQLAlchemyError

from indusguard_api import __version__
from indusguard_api.connectors import ConnectorCatalog
from indusguard_api.dashboard import (
    DashboardReader,
    PublicEvaluationDashboard,
    PublicRunTrace,
    SqlAlchemyDashboardReader,
)
from indusguard_api.schemas import (
    ConnectorSummary,
    HealthResponse,
    OperationSummary,
    ReadyResponse,
    VersionResponse,
)
from indusguard_api.settings import Settings


def create_app(
    *,
    connectors_dir: Path | None = None,
    settings: Settings | None = None,
    dashboard_reader: DashboardReader | None = None,
) -> FastAPI:
    """Cria uma instância isolada da aplicação.

    Usar uma factory, em vez de configurar tudo diretamente na variável global, permite que os
    testes apontem para diretórios temporários sem alterar variáveis globais ou o ambiente real.
    """

    current_settings = settings or (
        Settings(connectors_dir=connectors_dir) if connectors_dir else Settings()
    )
    catalog = ConnectorCatalog(current_settings.connectors_dir)
    reader = dashboard_reader or SqlAlchemyDashboardReader.from_url(current_settings.database_url)
    owns_reader = dashboard_reader is None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Carregar no startup implementa o comportamento fail-fast: um conector inválido impede
        # que o serviço anuncie readiness com um catálogo incompleto.
        catalog.load()
        try:
            yield
        finally:
            # Um reader injetado pertence ao chamador; somente a instância criada pela aplicação
            # encerra seu próprio pool de conexões.
            if owns_reader:
                await reader.close()

    application = FastAPI(
        title="IndusGuard API",
        summary="Runtime genérico para agentes conectados a APIs OpenAPI",
        version=__version__,
        lifespan=lifespan,
    )
    # ``app.state`` disponibiliza dependências já construídas às rotas sem recriá-las a cada
    # request. Em uma etapa futura isso poderá migrar para dependency injection explícita.
    application.state.settings = current_settings
    application.state.connector_catalog = catalog
    application.state.dashboard_reader = reader
    application.add_middleware(
        CORSMiddleware,
        allow_origins=current_settings.cors_allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["Accept", "Content-Type"],
    )

    @application.get(
        f"{current_settings.api_prefix}/health",
        response_model=HealthResponse,
        tags=["system"],
    )
    async def health() -> HealthResponse:
        """Liveness: confirma apenas que o processo HTTP consegue responder."""

        return HealthResponse()

    @application.get(
        f"{current_settings.api_prefix}/ready",
        response_model=ReadyResponse,
        tags=["system"],
    )
    async def ready(request: Request) -> ReadyResponse:
        """Readiness: confirma que o startup terminou e informa quantos conectores carregaram."""

        return ReadyResponse(connector_count=len(request.app.state.connector_catalog.list()))

    @application.get(
        f"{current_settings.api_prefix}/version",
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
        f"{current_settings.api_prefix}/connectors",
        response_model=list[ConnectorSummary],
        tags=["connectors"],
    )
    async def list_connectors(request: Request) -> list[ConnectorSummary]:
        """Lista integrações disponíveis sem revelar credenciais ou URLs resolvidas."""

        return request.app.state.connector_catalog.list()

    @application.get(
        f"{current_settings.api_prefix}/connectors/{{connector_id}}/operations",
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

    @application.get(
        f"{current_settings.api_prefix}/evaluations/latest",
        response_model=PublicEvaluationDashboard,
        tags=["dashboard"],
    )
    async def latest_evaluation(request: Request) -> PublicEvaluationDashboard:
        """Retorna a avaliação mais recente sem corpus, golden, mensagens ou evidências brutas."""

        try:
            evaluation = await request.app.state.dashboard_reader.latest_evaluation()
        except SQLAlchemyError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "DATASTORE_UNAVAILABLE",
                    "message": "Os dados do dashboard estão temporariamente indisponíveis.",
                },
            ) from error
        if evaluation is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "EVALUATION_NOT_FOUND",
                    "message": "Nenhuma avaliação foi registrada ainda.",
                },
            )
        return evaluation

    @application.get(
        f"{current_settings.api_prefix}/runs/{{run_id}}/trace",
        response_model=PublicRunTrace,
        tags=["dashboard"],
    )
    async def run_trace(run_id: str, request: Request) -> PublicRunTrace:
        """Expõe a timeline operacional sem carregar conteúdo livre do armazenamento interno."""

        try:
            trace = await request.app.state.dashboard_reader.trace(run_id)
        except SQLAlchemyError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "DATASTORE_UNAVAILABLE",
                    "message": "Os dados do dashboard estão temporariamente indisponíveis.",
                },
            ) from error
        if trace is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "TRACE_NOT_FOUND",
                    "message": "Trace não encontrado.",
                },
            )
        return trace

    return application


# O Uvicorn importa ``indusguard_api.main:app``. Manter a factory acima preserva testabilidade,
# enquanto esta instância oferece o ponto de entrada convencional para execução local.
app = create_app()
