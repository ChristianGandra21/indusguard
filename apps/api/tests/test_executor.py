"""Testes do primeiro corte vertical do executor HTTP genérico."""

import asyncio
from collections.abc import Callable, Mapping
from pathlib import Path

import httpx
import pytest
from conftest import REPOSITORY_ROOT

from indusguard_api.connectors import ConnectorCatalog
from indusguard_api.executor import HttpExecutor
from indusguard_api.schemas import (
    ExecutionArguments,
    ExecutionOutcome,
    OperationExecutionRequest,
    OperationExecutionResult,
)

RequestHandler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def catalog() -> ConnectorCatalog:
    """Carrega os mesmos conectores declarativos usados pela aplicação real."""

    loaded = ConnectorCatalog(REPOSITORY_ROOT / "connectors")
    loaded.load()
    return loaded


def _request(
    *,
    connector_id: str = "synthetic",
    operation_id: str = "getWidget",
    path: Mapping[str, object] | None = None,
) -> OperationExecutionRequest:
    """Produz pedidos pequenos para deixar cada teste focado em uma única regra."""

    return OperationExecutionRequest(
        connector_id=connector_id,
        operation_id=operation_id,
        arguments=ExecutionArguments(path=dict(path or {})),
    )


def _execute(
    catalog: ConnectorCatalog,
    request: OperationExecutionRequest,
    handler: RequestHandler,
    *,
    environment: Mapping[str, str] | None = None,
) -> OperationExecutionResult:
    """Executa async sem plugin extra e substitui a rede por ``MockTransport``."""

    async def run() -> OperationExecutionResult:
        transport = httpx.MockTransport(handler)
        resolved_environment = (
            {"SYNTHETIC_API_URL": "http://localhost:9000"} if environment is None else environment
        )
        async with httpx.AsyncClient(transport=transport) as client:
            executor = HttpExecutor(
                catalog,
                environment=resolved_environment,
                client=client,
            )
            return await executor.execute(request)

    return asyncio.run(run())


def test_executes_synthetic_get_and_returns_common_envelope(
    catalog: ConnectorCatalog,
) -> None:
    """Prova o corte vertical operationId -> OpenAPI -> HTTP -> envelope."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url == httpx.URL("http://localhost:9000/widgets/widget-123")
        return httpx.Response(200, json={"id": "widget-123", "status": "active"})

    result = _execute(catalog, _request(path={"widgetId": "widget-123"}), handler)

    assert result.outcome is ExecutionOutcome.EXECUTED
    assert result.status_code == 200
    assert result.data == {"id": "widget-123", "status": "active"}
    assert result.error is None
    assert result.latency_ms >= 0


def test_encodes_path_values_instead_of_creating_new_segments(
    catalog: ConnectorCatalog,
) -> None:
    """Uma barra enviada como dado não pode alterar o endpoint permitido."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path == b"/widgets/widget%2Fchild"
        return httpx.Response(200, json={"id": "widget/child", "status": "active"})

    result = _execute(catalog, _request(path={"widgetId": "widget/child"}), handler)

    assert result.outcome is ExecutionOutcome.EXECUTED


@pytest.mark.parametrize(
    ("path", "expected_code"),
    [
        ({}, "MISSING_PATH_ARGUMENT"),
        ({"widgetId": "widget-123", "extra": "value"}, "UNEXPECTED_PATH_ARGUMENT"),
        ({"widgetId": 123}, "INVALID_PATH_ARGUMENT"),
    ],
)
def test_blocks_invalid_path_arguments_before_network(
    catalog: ConnectorCatalog,
    path: dict[str, object],
    expected_code: str,
) -> None:
    """Ausência, excesso e tipo incorreto falham de forma determinística."""

    network_calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(200, json={})

    result = _execute(catalog, _request(path=path), handler)

    assert result.outcome is ExecutionOutcome.BLOCKED
    assert result.error.code == expected_code
    assert result.status_code is None
    assert network_calls == 0


@pytest.mark.parametrize(
    ("connector_id", "operation_id", "expected_code"),
    [
        ("missing", "getWidget", "CONNECTOR_NOT_FOUND"),
        ("synthetic", "missingOperation", "OPERATION_NOT_FOUND"),
    ],
)
def test_blocks_unknown_catalog_entries(
    catalog: ConnectorCatalog,
    connector_id: str,
    operation_id: str,
    expected_code: str,
) -> None:
    """O executor aceita identificadores do catálogo, nunca URLs fornecidas pela pessoa."""

    network_calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(200, json={})

    result = _execute(
        catalog,
        _request(connector_id=connector_id, operation_id=operation_id),
        handler,
    )

    assert result.outcome is ExecutionOutcome.BLOCKED
    assert result.error.code == expected_code
    assert network_calls == 0


def test_blocks_operation_disabled_by_default(tmp_path: Path) -> None:
    """Descobrir uma operação no OpenAPI não concede permissão para executá-la."""

    connector_dir = tmp_path / "disabled"
    connector_dir.mkdir()
    (connector_dir / "profile.yaml").write_text(
        """
id: disabled
name: Disabled
description: Fixture com operação não liberada
openapi: ./openapi.yaml
base_url_env: DISABLED_API_URL
allowed_base_urls: [http://disabled.local]
auth: {type: none}
operations: {}
""".strip(),
        encoding="utf-8",
    )
    (connector_dir / "openapi.yaml").write_text(
        """
openapi: 3.1.0
info: {title: Disabled, version: 1.0.0}
paths:
  /things/{thingId}:
    get:
      operationId: getThing
      parameters:
        - name: thingId
          in: path
          required: true
          schema: {type: string}
      responses:
        '200': {description: OK}
""".strip(),
        encoding="utf-8",
    )
    disabled_catalog = ConnectorCatalog(tmp_path)
    disabled_catalog.load()
    network_calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(200, json={})

    result = _execute(
        disabled_catalog,
        _request(connector_id="disabled", operation_id="getThing", path={"thingId": "1"}),
        handler,
        environment={"DISABLED_API_URL": "http://disabled.local"},
    )

    assert result.outcome is ExecutionOutcome.BLOCKED
    assert result.error.code == "OPERATION_DISABLED"
    assert network_calls == 0


def test_blocks_base_url_outside_connector_allowlist(catalog: ConnectorCatalog) -> None:
    """Mesmo uma variável de ambiente não autoriza um destino não aprovado no profile."""

    network_calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(200, json={})

    result = _execute(
        catalog,
        _request(path={"widgetId": "widget-123"}),
        handler,
        environment={"SYNTHETIC_API_URL": "https://example.invalid"},
    )

    assert result.outcome is ExecutionOutcome.BLOCKED
    assert result.error.code == "BASE_URL_NOT_ALLOWED"
    assert "example.invalid" not in result.error.message
    assert network_calls == 0


def test_blocks_malformed_base_url_without_leaking_it(catalog: ConnectorCatalog) -> None:
    """Uma configuração sintaticamente inválida também falha antes da rede."""

    network_calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(200, json={})

    result = _execute(
        catalog,
        _request(path={"widgetId": "widget-123"}),
        handler,
        environment={"SYNTHETIC_API_URL": "http://host:invalid-port"},
    )

    assert result.outcome is ExecutionOutcome.BLOCKED
    assert result.error.code == "INVALID_BASE_URL"
    assert "invalid-port" not in result.error.message
    assert network_calls == 0


def test_blocks_when_base_url_environment_variable_is_missing(
    catalog: ConnectorCatalog,
) -> None:
    """Configuração ausente é reportada sem tentar adivinhar um destino."""

    network_calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(200, json={})

    result = _execute(
        catalog,
        _request(path={"widgetId": "widget-123"}),
        handler,
        environment={},
    )

    assert result.outcome is ExecutionOutcome.BLOCKED
    assert result.error.code == "BASE_URL_ENV_MISSING"
    assert network_calls == 0


def test_keeps_write_operations_blocked_in_first_increment(catalog: ConnectorCatalog) -> None:
    """O PATCH sintético não é enviado enquanto a simulação de escritas não existir."""

    network_calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(200, json={})

    result = _execute(
        catalog,
        _request(operation_id="updateWidget", path={"widgetId": "widget-123"}),
        handler,
    )

    assert result.outcome is ExecutionOutcome.BLOCKED
    assert result.error.code == "METHOD_NOT_SUPPORTED"
    assert network_calls == 0


def test_normalizes_timeout_without_exposing_transport_details(
    catalog: ConnectorCatalog,
) -> None:
    """Falhas de transporte viram um erro estável e marcado como repetível."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("detalhe interno", request=request)

    result = _execute(catalog, _request(path={"widgetId": "widget-123"}), handler)

    assert result.outcome is ExecutionOutcome.FAILED
    assert result.error.code == "UPSTREAM_TIMEOUT"
    assert result.error.retryable is True
    assert "detalhe interno" not in result.error.message


def test_preserves_json_evidence_from_upstream_http_error(
    catalog: ConnectorCatalog,
) -> None:
    """Status e corpo JSON continuam disponíveis para futura decisão do agente."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"code": "UNAVAILABLE"})

    result = _execute(catalog, _request(path={"widgetId": "widget-123"}), handler)

    assert result.outcome is ExecutionOutcome.FAILED
    assert result.status_code == 503
    assert result.data == {"code": "UNAVAILABLE"}
    assert result.error.code == "UPSTREAM_HTTP_ERROR"
    assert result.error.retryable is True


def test_rejects_non_json_response(catalog: ConnectorCatalog) -> None:
    """O runtime REST JSON não transforma silenciosamente texto inesperado em evidência."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    result = _execute(catalog, _request(path={"widgetId": "widget-123"}), handler)

    assert result.outcome is ExecutionOutcome.FAILED
    assert result.status_code == 200
    assert result.error.code == "INVALID_JSON_RESPONSE"


def test_blocks_authentication_until_it_has_its_own_implementation(
    catalog: ConnectorCatalog,
) -> None:
    """O conector Tractian nunca é chamado anonimamente por falta de suporte parcial."""

    network_calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(200, json={})

    result = _execute(
        catalog,
        _request(connector_id="tractian", operation_id="getAsset"),
        handler,
        environment={"TRACTIAN_API_URL": "http://localhost:8000"},
    )

    assert result.outcome is ExecutionOutcome.BLOCKED
    assert result.error.code == "AUTH_NOT_SUPPORTED"
    assert network_calls == 0
