"""Aplicação HTTP que expõe o estado atual do núcleo IndusGuard.

As rotas permitem verificar catálogo e dashboard, além de iniciar uma run stateless pelo
``PublicRunHost``. O MCP, a policy e o executor continuam internos; a publicação atravessa
autenticação, quota, contexto confiável e observabilidade antes de alcançar o runtime.
"""

import os
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from indusguard_api import __version__
from indusguard_api.agent import AgentConfigurationError, AgentModelGateway
from indusguard_api.connectors import ConnectorCatalog
from indusguard_api.dashboard import (
    DashboardReader,
    PublicEvaluationDashboard,
    PublicRecentRunSummary,
    PublicRunTrace,
    SqlAlchemyDashboardReader,
)
from indusguard_api.groq_gateway import GroqAgentModelGateway
from indusguard_api.observability import Telemetry, mark_span_error, telemetry_from_settings
from indusguard_api.public_runs import (
    NoOpPublicRunQuota,
    PublicPlaygroundConfig,
    PublicRunError,
    PublicRunHost,
    PublicRunRequest,
    PublicRunResult,
    SqlAlchemyPublicRunQuota,
)
from indusguard_api.runtime_factory import InternalAgentHost, create_internal_agent_host
from indusguard_api.schemas import (
    ConnectorSummary,
    HealthResponse,
    OperationSummary,
    ReadyResponse,
    VersionResponse,
)
from indusguard_api.settings import Settings
from indusguard_api.synthetic_upstream import create_synthetic_upstream


class _PublicRunTransport(httpx.AsyncBaseTransport):
    """Roteia synthetic em memória e deixa outros destinos seguirem para a rede configurada."""

    def __init__(self) -> None:
        self._synthetic = httpx.ASGITransport(app=create_synthetic_upstream())
        self._network = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if (
            request.url.scheme == "http"
            and request.url.host in {"localhost", "127.0.0.1"}
            and request.url.port == 9000
        ):
            return await self._synthetic.handle_async_request(request)
        return await self._network.handle_async_request(request)

    async def aclose(self) -> None:
        await self._synthetic.aclose()
        await self._network.aclose()


def create_app(
    *,
    connectors_dir: Path | None = None,
    settings: Settings | None = None,
    dashboard_reader: DashboardReader | None = None,
    public_run_host: PublicRunHost | None = None,
    public_model_gateway: AgentModelGateway | None = None,
    telemetry: Telemetry | None = None,
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
    current_telemetry = telemetry or telemetry_from_settings(current_settings)
    owns_telemetry = telemetry is None
    runtime_host: InternalAgentHost | None = None
    quota_store: SqlAlchemyPublicRunQuota | None = None
    public_run_client: httpx.AsyncClient | None = None

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        nonlocal runtime_host, quota_store, public_run_client
        # Carregar no startup implementa o comportamento fail-fast: um conector inválido impede
        # que o serviço anuncie readiness com um catálogo incompleto.
        catalog.load()
        if public_run_host is None:
            runtime = None
            owner_token = (
                current_settings.owner_token.get_secret_value()
                if current_settings.owner_token is not None
                else None
            )
            quota = NoOpPublicRunQuota()
            if current_settings.public_runs_enabled:
                quota_store = SqlAlchemyPublicRunQuota.from_url(current_settings.database_url)
                quota = quota_store
                model_gateway = public_model_gateway
                if model_gateway is None:
                    try:
                        model_gateway = GroqAgentModelGateway()
                    except AgentConfigurationError:
                        # O serviço read-only continua disponível; readiness e POST mostram que o
                        # modelo precisa ser configurado, sem registrar exceção ou ambiente.
                        model_gateway = None
                if model_gateway is not None:
                    runtime_environment = {"SYNTHETIC_API_URL": "http://localhost:9000"}
                    tractian_api_url = os.environ.get("TRACTIAN_API_URL")
                    if tractian_api_url:
                        runtime_environment["TRACTIAN_API_URL"] = tractian_api_url
                    public_run_client = httpx.AsyncClient(transport=_PublicRunTransport())
                    runtime_host = create_internal_agent_host(
                        catalog=catalog,
                        model_gateway=model_gateway,
                        settings=current_settings,
                        http_client=public_run_client,
                        environment=runtime_environment,
                        telemetry=current_telemetry,
                    )
                    runtime = runtime_host.runtime
            application.state.public_run_host = PublicRunHost(
                catalog=catalog,
                runtime=runtime,
                quota=quota,
                enabled=current_settings.public_runs_enabled,
                owner_token=owner_token,
                public_connector_ids=current_settings.public_connector_ids,
                execution_mode=current_settings.execution_mode,
                rate_limit_per_hour=current_settings.public_run_rate_limit_per_hour,
                concurrency_limit=current_settings.public_run_concurrency,
                owner_id=current_settings.public_run_owner_id,
                telemetry=current_telemetry,
            )
        try:
            yield
        finally:
            if runtime_host is not None:
                await runtime_host.close()
            if public_run_client is not None:
                await public_run_client.aclose()
            if quota_store is not None:
                await quota_store.close()
            # Um reader injetado pertence ao chamador; somente a instância criada pela aplicação
            # encerra seu próprio pool de conexões.
            if owns_reader:
                await reader.close()
            if owns_telemetry:
                current_telemetry.force_flush()
                current_telemetry.shutdown()

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
    application.state.public_run_host = public_run_host
    application.state.telemetry = current_telemetry
    application.add_middleware(
        CORSMiddleware,
        allow_origins=current_settings.cors_allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Accept", "Authorization", "Content-Type"],
    )

    @application.exception_handler(RequestValidationError)
    async def public_run_validation_error(
        request: Request,
        error: RequestValidationError,
    ) -> Response:
        """Não ecoa valores inválidos que podem conter claims ou segredos do cliente."""

        if request.url.path == f"{current_settings.api_prefix}/runs":
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content={
                    "detail": {
                        "code": "CONTEXT_INVALID",
                        "message": "A solicitação contém campos ou valores não permitidos.",
                    }
                },
                headers={"Cache-Control": "no-store"},
            )
        return await request_validation_exception_handler(request, error)

    @application.middleware("http")
    async def trace_http_request(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Correlaciona HTTP e agente sem registrar corpo, query, token, IP ou headers."""

        started_at = perf_counter()
        with current_telemetry.start_span(
            "indusguard.http.server",
            {"http.request.method": request.method},
        ) as span:
            try:
                response = await call_next(request)
            except Exception:
                mark_span_error(span, "HTTP_UNHANDLED_ERROR")
                raise
            route = request.scope.get("route")
            route_path = getattr(route, "path", "unmatched")
            span.set_attribute("http.route", route_path)
            span.set_attribute("http.response.status_code", response.status_code)
            span.set_attribute(
                "http.server.request.duration_ms",
                max(0.0, round((perf_counter() - started_at) * 1000, 3)),
            )
            if response.status_code >= 500:
                mark_span_error(span, f"HTTP_{response.status_code}")
            return response

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
        """Confirma catálogo, banco migrado e host público quando ele está habilitado."""

        try:
            database_ready = await request.app.state.dashboard_reader.ready()
            host: PublicRunHost = request.app.state.public_run_host
            public_host_ready = await host.ready()
        except SQLAlchemyError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "SERVICE_NOT_READY",
                    "message": "O banco ainda não está pronto para receber tráfego.",
                },
            ) from error
        if not database_ready or not public_host_ready:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "SERVICE_NOT_READY",
                    "message": "Banco, migração ou host público ainda não estão prontos.",
                },
            )
        return ReadyResponse(
            connector_count=len(request.app.state.connector_catalog.list()),
            database_ready=True,
            public_run_host_ready=True,
        )

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
        f"{current_settings.api_prefix}/runs/recent",
        response_model=list[PublicRecentRunSummary],
        tags=["dashboard"],
    )
    async def recent_runs(
        request: Request,
        limit: int = Query(default=20, ge=1, le=100),
    ) -> list[PublicRecentRunSummary]:
        """Lista runs recentes por metadados seguros para navegação até o trace público."""

        try:
            return await request.app.state.dashboard_reader.recent_runs(limit=limit)
        except SQLAlchemyError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "DATASTORE_UNAVAILABLE",
                    "message": "Os dados do dashboard estão temporariamente indisponíveis.",
                },
            ) from error

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

    @application.get(
        f"{current_settings.api_prefix}/playground/config",
        response_model=PublicPlaygroundConfig,
        tags=["playground"],
    )
    async def playground_config(request: Request) -> PublicPlaygroundConfig:
        """Expõe capacidades públicas sem token, chave do modelo ou configuração interna."""

        host: PublicRunHost | None = request.app.state.public_run_host
        if host is None:
            return PublicPlaygroundConfig(
                enabled=False,
                model_configured=False,
                execution_mode=current_settings.execution_mode,
                connectors=[],
                rate_limit_per_hour=current_settings.public_run_rate_limit_per_hour,
                concurrency_limit=current_settings.public_run_concurrency,
            )
        return host.config()

    @application.post(
        f"{current_settings.api_prefix}/runs",
        response_model=PublicRunResult,
        tags=["playground"],
    )
    async def create_public_run(
        payload: PublicRunRequest,
        request: Request,
        response: Response,
    ) -> PublicRunResult:
        """Entrega uma solicitação ao host profundo sem construir claims na camada HTTP."""

        response.headers["Cache-Control"] = "no-store"
        host: PublicRunHost | None = request.app.state.public_run_host
        if host is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "PUBLIC_RUNS_DISABLED",
                    "message": "As execuções públicas estão desabilitadas.",
                },
                headers={"Cache-Control": "no-store"},
            )
        try:
            return await host.execute(request.headers.get("authorization"), payload)
        except PublicRunError as error:
            headers = {"Cache-Control": "no-store"}
            if error.retry_after_seconds is not None:
                headers["Retry-After"] = str(error.retry_after_seconds)
            raise HTTPException(
                status_code=error.status_code,
                detail={"code": error.code.value, "message": str(error)},
                headers=headers,
            ) from error

    return application


# O Uvicorn importa ``indusguard_api.main:app``. Manter a factory acima preserva testabilidade,
# enquanto esta instância oferece o ponto de entrada convencional para execução local.
app = create_app()
