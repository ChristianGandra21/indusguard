"""Testes dos três primeiros cortes verticais do executor HTTP genérico."""

import asyncio
from collections.abc import Callable, Mapping
from pathlib import Path
from textwrap import dedent, indent
from typing import Literal

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
BODY_NOT_SET = object()


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
    query: Mapping[str, object] | None = None,
    headers: Mapping[str, object] | None = None,
    body: object = BODY_NOT_SET,
    context: Mapping[str, object] | None = None,
) -> OperationExecutionRequest:
    """Produz pedidos pequenos para deixar cada teste focado em uma única regra."""

    argument_values: dict[str, object] = {
        "path": dict(path or {}),
        "query": dict(query or {}),
        "headers": dict(headers or {}),
    }
    if body is not BODY_NOT_SET:
        argument_values["body"] = body
    return OperationExecutionRequest(
        connector_id=connector_id,
        operation_id=operation_id,
        arguments=ExecutionArguments.model_validate(argument_values),
        context=dict(context or {}),
    )


def _execute(
    catalog: ConnectorCatalog,
    request: OperationExecutionRequest,
    handler: RequestHandler,
    *,
    environment: Mapping[str, str] | None = None,
    execution_mode: Literal["simulate", "execute"] = "simulate",
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
                execution_mode=execution_mode,
                # Os testes verificam a política de retry sem introduzir espera real.
                retry_base_delay_seconds=0,
            )
            return await executor.execute(request)

    return asyncio.run(run())


def _temporary_catalog(
    tmp_path: Path,
    *,
    auth_yaml: str = "type: none",
    method: Literal["get", "patch"] = "get",
    policy_yaml: str = "idempotent: true\nmax_retries: 2",
    request_body_yaml: str = "",
) -> ConnectorCatalog:
    """Cria um conector declarativo pequeno para testar políticas sem código de domínio."""

    connector_dir = tmp_path / "test_connector"
    connector_dir.mkdir()
    access = "read" if method == "get" else "write"
    auth_block = indent(dedent(auth_yaml).strip(), "  ")
    policy_block = indent(dedent(policy_yaml).strip(), "    ")
    (connector_dir / "profile.yaml").write_text(
        (
            "id: test_connector\n"
            "name: Test connector\n"
            "description: Conector temporário dos testes do executor\n"
            "openapi: ./openapi.yaml\n"
            "base_url_env: TEST_CONNECTOR_URL\n"
            "allowed_base_urls: [http://test.local]\n"
            "auth:\n"
            f"{auth_block}\n"
            "operations:\n"
            "  accessItem:\n"
            "    enabled: true\n"
            f"    access: {access}\n"
            f"{policy_block}\n"
        ),
        encoding="utf-8",
    )
    body_block = (
        f"{indent(dedent(request_body_yaml).strip(), '      ')}\n" if request_body_yaml else ""
    )
    (connector_dir / "openapi.yaml").write_text(
        (
            "openapi: 3.1.0\n"
            "info: {title: Test API, version: 1.0.0}\n"
            "paths:\n"
            "  /items/{itemId}:\n"
            f"    {method}:\n"
            "      operationId: accessItem\n"
            "      parameters:\n"
            "        - name: itemId\n"
            "          in: path\n"
            "          required: true\n"
            "          schema: {type: string}\n"
            f"{body_block}"
            "      responses:\n"
            "        '200':\n"
            "          description: Success\n"
            "          content:\n"
            "            application/json:\n"
            "              schema: {type: object}\n"
        ),
        encoding="utf-8",
    )
    loaded = ConnectorCatalog(tmp_path)
    loaded.load()
    return loaded


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
    assert result.attempts == 1
    assert result.simulation is None
    assert result.latency_ms >= 0


def test_serializes_query_array_and_declared_header(catalog: ConnectorCatalog) -> None:
    """O synthetic prova argumentos novos sem adicionar lógica específica no Python."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(
            "http://localhost:9000/widgets/widget-123?labels=critical&labels=monitored"
        )
        assert request.headers["x-request-id"] == "req-001"
        return httpx.Response(200, json={"id": "widget-123", "status": "active"})

    result = _execute(
        catalog,
        _request(
            path={"widgetId": "widget-123"},
            query={"labels": ["critical", "monitored"]},
            headers={"X-Request-ID": "req-001"},
        ),
        handler,
    )

    assert result.outcome is ExecutionOutcome.EXECUTED


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


def test_simulates_valid_write_without_network(catalog: ConnectorCatalog) -> None:
    """Um PATCH válido gera uma prévia tipada, mas nunca chega à rede no modo seguro."""

    network_calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(200, json={})

    result = _execute(
        catalog,
        _request(
            operation_id="updateWidget",
            path={"widgetId": "widget-123"},
            body={
                "status": "inactive",
                "justification": "alteração solicitada para manutenção preventiva",
            },
        ),
        handler,
    )

    assert result.outcome is ExecutionOutcome.SIMULATED
    assert result.error is None
    assert result.attempts == 0
    assert result.simulation.method == "PATCH"
    assert result.simulation.path == "/widgets/widget-123"
    assert result.simulation.body_present is True
    assert result.simulation.body["status"] == "inactive"
    assert network_calls == 0


def test_blocks_real_write_when_http_executor_is_used_directly(catalog: ConnectorCatalog) -> None:
    """Selecionar execute não permite contornar o GuardedExecutor e sua decisão política."""

    network_calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(200, json={})

    result = _execute(
        catalog,
        _request(
            operation_id="updateWidget",
            path={"widgetId": "widget-123"},
            body={
                "status": "inactive",
                "justification": "alteração solicitada para manutenção preventiva",
            },
        ),
        handler,
        execution_mode="execute",
    )

    assert result.outcome is ExecutionOutcome.BLOCKED
    assert result.error.code == "WRITE_POLICY_REQUIRED"
    assert result.simulation is None
    assert result.attempts == 0
    assert network_calls == 0


def test_simulation_redacts_body_and_does_not_read_external_credentials(tmp_path: Path) -> None:
    """A prévia funciona offline e mostra a forma da autenticação, nunca seu valor."""

    catalog = _temporary_catalog(
        tmp_path,
        auth_yaml="""
        type: api_key_header
        name: x-api-key
        env: TEST_API_SECRET
        """,
        method="patch",
        policy_yaml="""
        idempotent: false
        max_retries: 0
        redact_fields: [internal_note]
        """,
        request_body_yaml="""
        requestBody:
          required: true
          content:
            application/json:
              schema:
                type: object
                additionalProperties: false
                required: [status, internal_note]
                properties:
                  status: {type: string}
                  internal_note: {type: string}
        """,
    )
    network_calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(200, json={})

    body = {"status": "inactive", "internal_note": "não deve aparecer"}
    result = _execute(
        catalog,
        _request(
            connector_id="test_connector",
            operation_id="accessItem",
            path={"itemId": "item-1"},
            body=body,
        ),
        handler,
        # Nem a URL do upstream nem a API key são necessárias para uma simulação.
        environment={},
    )

    assert result.outcome is ExecutionOutcome.SIMULATED
    assert result.simulation.auth_type == "api_key_header"
    assert result.simulation.header_names == ["x-api-key"]
    assert result.simulation.body == {
        "status": "inactive",
        "internal_note": "[REDACTED]",
    }
    assert body["internal_note"] == "não deve aparecer"
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
    assert result.attempts == 3
    assert "detalhe interno" not in result.error.message


def test_normalizes_connection_error_without_exposing_destination(
    catalog: ConnectorCatalog,
) -> None:
    """Um erro de conexão também usa código estável e não reproduz a exceção."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("host interno indisponível", request=request)

    result = _execute(catalog, _request(path={"widgetId": "widget-123"}), handler)

    assert result.outcome is ExecutionOutcome.FAILED
    assert result.error.code == "UPSTREAM_CONNECTION_ERROR"
    assert result.error.retryable is True
    assert result.attempts == 3
    assert "host interno" not in result.error.message


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
    assert result.attempts == 3


@pytest.mark.parametrize("retryable_status", [429, 503])
def test_retries_idempotent_read_until_it_succeeds(
    catalog: ConnectorCatalog,
    retryable_status: int,
) -> None:
    """429 e 5xx podem ser transitórios e respeitam o limite declarado no profile."""

    network_calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        if network_calls < 3:
            return httpx.Response(retryable_status, json={"attempt": network_calls})
        return httpx.Response(200, json={"status": "recovered"})

    result = _execute(catalog, _request(path={"widgetId": "widget-123"}), handler)

    assert result.outcome is ExecutionOutcome.EXECUTED
    assert result.data == {"status": "recovered"}
    assert result.attempts == 3
    assert network_calls == 3


def test_does_not_retry_regular_client_error(catalog: ConnectorCatalog) -> None:
    """Um 400 é determinístico para aquele request e repeti-lo só consumiria quota."""

    network_calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(400, json={"code": "INVALID_INPUT"})

    result = _execute(catalog, _request(path={"widgetId": "widget-123"}), handler)

    assert result.outcome is ExecutionOutcome.FAILED
    assert result.error.retryable is False
    assert result.attempts == 1
    assert network_calls == 1


def test_does_not_retry_operation_that_is_not_idempotent(tmp_path: Path) -> None:
    """max_retries sozinho não concede permissão para repetir efeitos possivelmente únicos."""

    catalog = _temporary_catalog(
        tmp_path,
        policy_yaml="idempotent: false\nmax_retries: 2",
    )
    network_calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(503, json={"code": "UNAVAILABLE"})

    result = _execute(
        catalog,
        _request(
            connector_id="test_connector",
            operation_id="accessItem",
            path={"itemId": "item-1"},
        ),
        handler,
        environment={"TEST_CONNECTOR_URL": "http://test.local"},
    )

    assert result.outcome is ExecutionOutcome.FAILED
    assert result.error.retryable is True
    assert result.attempts == 1
    assert network_calls == 1


def test_redacts_configured_fields_recursively_from_upstream_data(tmp_path: Path) -> None:
    """Campos do profile desaparecem em qualquer profundidade antes de formar o envelope."""

    catalog = _temporary_catalog(
        tmp_path,
        policy_yaml="""
        idempotent: true
        max_retries: 0
        redact_fields: [internal_note]
        """,
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "internal_note": "segredo de nível superior",
                "nested": [
                    {"internal_note": "segredo interno", "safe": "permanece"},
                ],
            },
        )

    result = _execute(
        catalog,
        _request(
            connector_id="test_connector",
            operation_id="accessItem",
            path={"itemId": "item-1"},
        ),
        handler,
        environment={"TEST_CONNECTOR_URL": "http://test.local"},
    )

    assert result.data == {
        "internal_note": "[REDACTED]",
        "nested": [{"internal_note": "[REDACTED]", "safe": "permanece"}],
    }


def test_rejects_non_json_response(catalog: ConnectorCatalog) -> None:
    """O runtime REST JSON não transforma silenciosamente texto inesperado em evidência."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    result = _execute(catalog, _request(path={"widgetId": "widget-123"}), handler)

    assert result.outcome is ExecutionOutcome.FAILED
    assert result.status_code == 200
    assert result.error.code == "INVALID_JSON_RESPONSE"


def test_executes_tractian_get_with_ref_query_and_context_auth(
    catalog: ConnectorCatalog,
) -> None:
    """Prova path + query por $ref + header derivado do contexto no conector real."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url == httpx.URL("http://localhost:8000/assets/asset_M101?seed=case-01")
        assert request.headers["x-user-id"] == "usr_001"
        return httpx.Response(200, json={"status": "complete", "data": {"id": "asset_M101"}})

    result = _execute(
        catalog,
        _request(
            connector_id="tractian",
            operation_id="getAsset",
            path={"assetId": "asset_M101"},
            query={"seed": "case-01"},
            context={"user_id": "usr_001"},
        ),
        handler,
        environment={"TRACTIAN_API_URL": "http://localhost:8000"},
    )

    assert result.outcome is ExecutionOutcome.EXECUTED
    assert result.status_code == 200
    assert result.data["data"]["id"] == "asset_M101"


@pytest.mark.parametrize(
    ("auth_yaml", "expected_location"),
    [
        (
            """
            type: api_key_header
            name: x-api-key
            env: TEST_API_SECRET
            """,
            "header",
        ),
        (
            """
            type: api_key_query
            name: api_key
            env: TEST_API_SECRET
            """,
            "query",
        ),
        (
            """
            type: bearer
            env: TEST_API_SECRET
            """,
            "bearer",
        ),
    ],
)
def test_executes_all_environment_authentication_modes_without_leaking_secret(
    tmp_path: Path,
    auth_yaml: str,
    expected_location: str,
) -> None:
    """API key e Bearer chegam ao upstream, mas são removidos até de uma resposta refletida."""

    secret = "credential-that-must-not-leak"
    catalog = _temporary_catalog(tmp_path, auth_yaml=auth_yaml)

    def handler(request: httpx.Request) -> httpx.Response:
        if expected_location == "header":
            assert request.headers["x-api-key"] == secret
        elif expected_location == "query":
            assert request.url.params["api_key"] == secret
        else:
            assert request.headers["authorization"] == f"Bearer {secret}"
        return httpx.Response(
            200,
            json={"echo": secret, "nested": [f"received={secret}"]},
        )

    result = _execute(
        catalog,
        _request(
            connector_id="test_connector",
            operation_id="accessItem",
            path={"itemId": "item-1"},
        ),
        handler,
        environment={
            "TEST_CONNECTOR_URL": "http://test.local",
            "TEST_API_SECRET": secret,
        },
    )

    assert result.outcome is ExecutionOutcome.EXECUTED
    assert result.attempts == 1
    assert secret not in result.model_dump_json()
    assert result.data == {
        "echo": "[REDACTED]",
        "nested": ["received=[REDACTED]"],
    }


@pytest.mark.parametrize(
    "auth_yaml",
    [
        "type: api_key_header\nname: x-api-key\nenv: TEST_API_SECRET",
        "type: api_key_query\nname: api_key\nenv: TEST_API_SECRET",
        "type: bearer\nenv: TEST_API_SECRET",
    ],
)
def test_blocks_environment_authentication_when_secret_is_missing(
    tmp_path: Path,
    auth_yaml: str,
) -> None:
    """Uma credencial ausente falha antes da rede sem adivinhar valor ou destino."""

    catalog = _temporary_catalog(tmp_path, auth_yaml=auth_yaml)
    network_calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(200, json={})

    result = _execute(
        catalog,
        _request(
            connector_id="test_connector",
            operation_id="accessItem",
            path={"itemId": "item-1"},
        ),
        handler,
        environment={"TEST_CONNECTOR_URL": "http://test.local"},
    )

    assert result.outcome is ExecutionOutcome.BLOCKED
    assert result.error.code == "AUTH_ENV_MISSING"
    assert result.attempts == 0
    assert network_calls == 0


@pytest.mark.parametrize(
    ("auth_yaml", "headers", "query", "expected_code"),
    [
        (
            "type: api_key_header\nname: x-api-key\nenv: TEST_API_SECRET",
            {"x-api-key": "forged"},
            {},
            "RESERVED_AUTH_HEADER",
        ),
        (
            "type: bearer\nenv: TEST_API_SECRET",
            {"Authorization": "forged"},
            {},
            "RESERVED_AUTH_HEADER",
        ),
        (
            "type: api_key_query\nname: api_key\nenv: TEST_API_SECRET",
            {},
            {"api_key": "forged"},
            "RESERVED_AUTH_QUERY",
        ),
    ],
)
def test_blocks_authentication_values_supplied_as_arguments(
    tmp_path: Path,
    auth_yaml: str,
    headers: dict[str, object],
    query: dict[str, object],
    expected_code: str,
) -> None:
    """O agente escolhe argumentos da operação, nunca a identidade usada no transporte."""

    catalog = _temporary_catalog(tmp_path, auth_yaml=auth_yaml)
    network_calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(200, json={})

    result = _execute(
        catalog,
        _request(
            connector_id="test_connector",
            operation_id="accessItem",
            path={"itemId": "item-1"},
            headers=headers,
            query=query,
        ),
        handler,
        environment={
            "TEST_CONNECTOR_URL": "http://test.local",
            "TEST_API_SECRET": "real-secret",
        },
    )

    assert result.outcome is ExecutionOutcome.BLOCKED
    assert result.error.code == expected_code
    assert network_calls == 0


@pytest.mark.parametrize(
    ("execution_request", "expected_code"),
    [
        (
            _request(
                connector_id="tractian",
                operation_id="getAsset",
                path={"assetId": "asset_M101"},
            ),
            "AUTH_CONTEXT_MISSING",
        ),
        (
            _request(
                connector_id="tractian",
                operation_id="getAsset",
                path={"assetId": "asset_M101"},
                headers={"x-user-id": "forged-user"},
                context={"user_id": "usr_001"},
            ),
            "RESERVED_AUTH_HEADER",
        ),
        (
            _request(
                connector_id="tractian",
                operation_id="getAsset",
                path={"assetId": "asset_M101"},
                context={"user_id": "usr_001\nforged"},
            ),
            "INVALID_AUTH_CONTEXT",
        ),
    ],
)
def test_blocks_missing_or_forged_context_authentication(
    catalog: ConnectorCatalog,
    execution_request: OperationExecutionRequest,
    expected_code: str,
) -> None:
    """O header de identidade precisa ser derivado exclusivamente do contexto."""

    network_calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(200, json={})

    result = _execute(
        catalog,
        execution_request,
        handler,
        environment={"TRACTIAN_API_URL": "http://localhost:8000"},
    )

    assert result.outcome is ExecutionOutcome.BLOCKED
    assert result.error.code == expected_code
    assert network_calls == 0


@pytest.mark.parametrize(
    ("query", "expected_code"),
    [
        ({}, "MISSING_QUERY_ARGUMENT"),
        ({"q": "bearing", "type": "unknown"}, "INVALID_QUERY_ARGUMENT"),
        ({"q": "bearing", "extra": "value"}, "UNEXPECTED_QUERY_ARGUMENT"),
    ],
)
def test_validates_required_enum_and_unknown_query_arguments(
    catalog: ConnectorCatalog,
    query: dict[str, object],
    expected_code: str,
) -> None:
    """A operação searchKnowledge demonstra query obrigatória, enum e campo extra."""

    network_calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(200, json={})

    result = _execute(
        catalog,
        _request(
            connector_id="tractian",
            operation_id="searchKnowledge",
            query=query,
            context={"user_id": "usr_001"},
        ),
        handler,
        environment={"TRACTIAN_API_URL": "http://localhost:8000"},
    )

    assert result.outcome is ExecutionOutcome.BLOCKED
    assert result.error.code == expected_code
    assert network_calls == 0


@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        (BODY_NOT_SET, "MISSING_REQUEST_BODY"),
        (
            {"status": "invalid", "justification": "alteração detalhada para manutenção"},
            "INVALID_REQUEST_BODY",
        ),
        (
            {"status": "active", "justification": "curta"},
            "INVALID_REQUEST_BODY",
        ),
    ],
)
def test_validates_required_body_before_write_branch(
    catalog: ConnectorCatalog,
    body: object,
    expected_code: str,
) -> None:
    """Body ausente, enum inválido e justificativa curta falham antes de qualquer PATCH."""

    network_calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(200, json={})

    result = _execute(
        catalog,
        _request(
            operation_id="updateWidget",
            path={"widgetId": "widget-123"},
            body=body,
        ),
        handler,
    )

    assert result.outcome is ExecutionOutcome.BLOCKED
    assert result.error.code == expected_code
    assert network_calls == 0


@pytest.mark.parametrize(
    ("body", "expected_outcome", "expected_code"),
    [
        (
            {
                "justification": "mudança aprovada para adequar a criticidade do ativo",
                "changes": {"criticality": "high"},
            },
            ExecutionOutcome.SIMULATED,
            None,
        ),
        (
            {
                "justification": "mudança aprovada para adequar a criticidade do ativo",
                "changes": {"criticality": "impossible"},
            },
            ExecutionOutcome.BLOCKED,
            "INVALID_REQUEST_BODY",
        ),
    ],
)
def test_resolves_nested_body_schema_references_from_tractian(
    catalog: ConnectorCatalog,
    body: dict[str, object],
    expected_outcome: ExecutionOutcome,
    expected_code: str | None,
) -> None:
    """O allOf com ActionRequest e AssetConfig é validado usando a raiz OpenAPI local."""

    network_calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(200, json={})

    result = _execute(
        catalog,
        _request(
            connector_id="tractian",
            operation_id="updateAssetConfig",
            path={"assetId": "asset_M101"},
            body=body,
            context={"user_id": "usr_001"},
        ),
        handler,
        environment={"TRACTIAN_API_URL": "http://localhost:8000"},
    )

    assert result.outcome is expected_outcome
    assert (result.error.code if result.error else None) == expected_code
    assert network_calls == 0


def test_rejects_body_when_openapi_operation_does_not_declare_one(
    catalog: ConnectorCatalog,
) -> None:
    """Um body extra não é ignorado nem enviado por conveniência."""

    network_calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(200, json={})

    result = _execute(
        catalog,
        _request(path={"widgetId": "widget-123"}, body={"unexpected": True}),
        handler,
    )

    assert result.outcome is ExecutionOutcome.BLOCKED
    assert result.error.code == "UNEXPECTED_REQUEST_BODY"
    assert network_calls == 0
