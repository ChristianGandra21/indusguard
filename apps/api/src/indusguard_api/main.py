"""Aplicação HTTP do IndusGuard."""

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
    settings = Settings(connectors_dir=connectors_dir) if connectors_dir else Settings()
    catalog = ConnectorCatalog(settings.connectors_dir)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        catalog.load()
        yield

    application = FastAPI(
        title="IndusGuard API",
        summary="Runtime genérico para agentes conectados a APIs OpenAPI",
        version=__version__,
        lifespan=lifespan,
    )
    application.state.settings = settings
    application.state.connector_catalog = catalog

    @application.get(
        f"{settings.api_prefix}/health",
        response_model=HealthResponse,
        tags=["system"],
    )
    async def health() -> HealthResponse:
        return HealthResponse()

    @application.get(
        f"{settings.api_prefix}/ready",
        response_model=ReadyResponse,
        tags=["system"],
    )
    async def ready(request: Request) -> ReadyResponse:
        return ReadyResponse(connector_count=len(request.app.state.connector_catalog.list()))

    @application.get(
        f"{settings.api_prefix}/version",
        response_model=VersionResponse,
        tags=["system"],
    )
    async def version(request: Request) -> VersionResponse:
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
        return request.app.state.connector_catalog.list()

    @application.get(
        f"{settings.api_prefix}/connectors/{{connector_id}}/operations",
        response_model=list[OperationSummary],
        tags=["connectors"],
    )
    async def list_operations(connector_id: str, request: Request) -> list[OperationSummary]:
        connector = request.app.state.connector_catalog.get(connector_id)
        if connector is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"conector '{connector_id}' não encontrado",
            )
        return connector.operations

    return application


app = create_app()
