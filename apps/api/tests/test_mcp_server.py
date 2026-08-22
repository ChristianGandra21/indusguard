"""Testes do protocolo MCP sobre o fluxo protegido do IndusGuard."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from textwrap import dedent
from typing import Any, Literal

import httpx
import pytest
from conftest import REPOSITORY_ROOT
from jsonschema.validators import validator_for
from mcp import Client
from mcp_types import Tool

from indusguard_api.connectors import ConnectorCatalog
from indusguard_api.executor import HttpExecutor
from indusguard_api.mcp_server import (
    McpServerConfigurationError,
    TrustedPolicySignals,
    create_mcp_server,
)
from indusguard_api.policy import GuardedExecutor, PolicyEngine
from indusguard_api.schemas import (
    ExecutionArguments,
    PolicyConfirmation,
    PolicyPrincipal,
)


@pytest.fixture
def catalog() -> ConnectorCatalog:
    """Carrega os conectores reais para testar a descoberta como um cliente faria."""

    loaded = ConnectorCatalog(REPOSITORY_ROOT / "connectors")
    loaded.load()
    return loaded


class StaticTrustedContextProvider:
    """Provider explícito que representa o runtime autenticado nos testes."""

    async def resolve(self, **_: object) -> TrustedPolicySignals:
        return TrustedPolicySignals()


class RecordingTrustedContextProvider:
    """Registra a fronteira confiável para provar ordem e conteúdo da chamada."""

    def __init__(self, signals: TrustedPolicySignals | None = None) -> None:
        self.signals = signals or TrustedPolicySignals()
        self.calls: list[dict[str, Any]] = []

    async def resolve(self, **values: Any) -> TrustedPolicySignals:
        self.calls.append(values)
        return self.signals


class FailingTrustedContextProvider:
    """Simula indisponibilidade sem permitir que detalhes internos escapem ao cliente."""

    async def resolve(self, **_: Any) -> TrustedPolicySignals:
        raise RuntimeError("token-super-secreto não pode aparecer")


def _guarded_executor(catalog: ConnectorCatalog) -> GuardedExecutor:
    """Compõe o fluxo real; a listagem não deve chegar ao transporte HTTP."""

    http_executor = HttpExecutor(catalog, execution_mode="simulate")
    return GuardedExecutor(
        PolicyEngine(catalog, execution_mode="simulate"),
        http_executor,
    )


def _list_tools(server: object) -> list[Tool]:
    """Usa o cliente do SDK para observar somente o contrato público do servidor."""

    async def list_tools() -> list[Tool]:
        async with Client(server, mode="legacy") as client:
            result = await client.list_tools()
            return result.tools

    return asyncio.run(list_tools())


def _invoke_tool(
    catalog: ConnectorCatalog,
    *,
    name: str,
    arguments: dict[str, Any],
    provider: Any,
    execution_mode: Literal["simulate", "execute"] = "simulate",
    upstream: Callable[[httpx.Request], httpx.Response] | None = None,
) -> tuple[Any, list[httpx.Request]]:
    """Chama a tool com cliente real e substitui somente a fronteira HTTP externa."""

    upstream_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        upstream_requests.append(request)
        if upstream is not None:
            return upstream(request)
        return httpx.Response(200, json={"id": "widget-1", "status": "active"})

    async def call_tool() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            guarded = GuardedExecutor(
                PolicyEngine(catalog, execution_mode=execution_mode),
                HttpExecutor(
                    catalog,
                    environment={
                        "SYNTHETIC_API_URL": "http://localhost:9000",
                        "TRACTIAN_API_URL": "http://localhost:8000",
                    },
                    client=http_client,
                    execution_mode=execution_mode,
                    retry_base_delay_seconds=0,
                ),
            )
            server = create_mcp_server(catalog, guarded, provider)
            async with Client(server, mode="legacy") as mcp_client:
                return await mcp_client.call_tool(name, arguments)

    return asyncio.run(call_tool()), upstream_requests


def _temporary_connector_catalog(
    tmp_path: Path,
    *,
    operation_id: str = "getThing",
    enabled: bool = True,
    include_cookie: bool = False,
) -> ConnectorCatalog:
    """Cria uma API externa mínima usando somente os três arquivos de um conector."""

    connector_dir = tmp_path / "extension"
    connector_dir.mkdir()
    (connector_dir / "profile.yaml").write_text(
        dedent(
            f"""
            id: extension
            name: Extension API
            description: Conector temporário sem código Python específico
            openapi: ./openapi.yaml
            base_url_env: EXTENSION_API_URL
            allowed_base_urls: [http://localhost:9100]
            auth: {{type: none}}
            operations:
              "{operation_id}":
                enabled: {str(enabled).lower()}
                access: read
                risk: low
                idempotent: true
            """
        ).strip(),
        encoding="utf-8",
    )
    openapi = dedent(
        f"""
            openapi: 3.1.0
            info: {{title: Extension, version: 1.0.0}}
            paths:
              /things/{{thingId}}:
                get:
                  operationId: "{operation_id}"
                  parameters:
                    - name: thingId
                      in: path
                      required: true
                      schema:
                        $ref: '#/components/schemas/Identifier'
                  responses:
                    '200': {{description: OK}}
            components:
              schemas:
                Identifier:
                  type: string
                  pattern: '^thing-[0-9]+$'
            """
    ).strip()
    if include_cookie:
        openapi = openapi.replace(
            "      responses:",
            "        - name: session\n"
            "          in: cookie\n"
            "          required: false\n"
            "          schema: {type: string}\n"
            "      responses:",
        )
    (connector_dir / "openapi.yaml").write_text(openapi, encoding="utf-8")
    loaded = ConnectorCatalog(tmp_path)
    loaded.load()
    return loaded


def test_lists_all_enabled_connector_operations_as_stable_tools(
    catalog: ConnectorCatalog,
) -> None:
    """O cliente MCP enxerga uma tool por operação habilitada, sem conhecer Python interno."""

    server = create_mcp_server(
        catalog,
        _guarded_executor(catalog),
        StaticTrustedContextProvider(),
    )

    names = [tool.name for tool in _list_tools(server)]

    assert len(names) == 20
    assert names == sorted(names)
    assert "synthetic.getWidget" in names
    assert "synthetic.updateWidget" in names
    assert "tractian.getAsset" in names
    assert "tractian.updateAssetConfig" in names


def test_exposes_specific_argument_schemas_and_annotations(catalog: ConnectorCatalog) -> None:
    """Schemas orientam o agente sem permitir que ele controle sinais confiáveis."""

    server = create_mcp_server(
        catalog,
        _guarded_executor(catalog),
        StaticTrustedContextProvider(),
    )
    tools = {tool.name: tool for tool in _list_tools(server)}

    read_tool = tools["synthetic.getWidget"]
    assert read_tool.annotations is not None
    assert read_tool.annotations.read_only_hint is True
    assert read_tool.annotations.destructive_hint is False
    assert read_tool.annotations.idempotent_hint is True
    assert read_tool.annotations.open_world_hint is True
    assert read_tool.input_schema == {
        "type": "object",
        "properties": {
            "path": {
                "type": "object",
                "properties": {"widgetId": {"type": "string"}},
                "required": ["widgetId"],
                "additionalProperties": False,
            },
            "query": {
                "type": "object",
                "properties": {"labels": {"type": "array", "items": {"type": "string"}}},
                "additionalProperties": False,
            },
            "headers": {
                "type": "object",
                "properties": {"x-request-id": {"type": "string", "minLength": 1}},
                "additionalProperties": False,
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    write_tool = tools["synthetic.updateWidget"]
    assert write_tool.annotations is not None
    assert write_tool.annotations.read_only_hint is False
    assert write_tool.annotations.destructive_hint is True
    assert write_tool.annotations.idempotent_hint is False
    assert write_tool.annotations.open_world_hint is True
    assert write_tool.output_schema is not None
    assert set(write_tool.output_schema["properties"]) == {"policy", "execution"}
    assert write_tool.input_schema["required"] == ["path", "body"]
    assert write_tool.input_schema["properties"]["body"] == {
        "type": "object",
        "required": ["status", "justification"],
        "properties": {
            "status": {"type": "string", "enum": ["active", "inactive"]},
            "justification": {"type": "string", "minLength": 20},
        },
    }

    serialized_schema = str([tool.input_schema for tool in tools.values()])
    for forbidden in (
        "principal",
        "permissions",
        "scopes",
        "confirmation",
        "direct_request",
        "connector_id",
        "operation_id",
        "base_url",
        "credential",
        "TRACTIAN_API_URL",
        "SYNTHETIC_API_URL",
        "x-user-id",
    ):
        assert forbidden not in serialized_schema


def test_calls_allowed_read_through_full_guarded_flow(catalog: ConnectorCatalog) -> None:
    """Uma leitura válida cruza protocolo, provider, policy e executor exatamente uma vez."""

    upstream_requests: list[httpx.Request] = []
    provider = RecordingTrustedContextProvider()

    def upstream(request: httpx.Request) -> httpx.Response:
        upstream_requests.append(request)
        return httpx.Response(200, json={"id": "widget-1", "status": "active"})

    async def call_tool() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http_client:
            guarded = GuardedExecutor(
                PolicyEngine(catalog, execution_mode="simulate"),
                HttpExecutor(
                    catalog,
                    environment={"SYNTHETIC_API_URL": "http://localhost:9000"},
                    client=http_client,
                    execution_mode="simulate",
                ),
            )
            server = create_mcp_server(catalog, guarded, provider)
            async with Client(server, mode="legacy") as mcp_client:
                return await mcp_client.call_tool(
                    "synthetic.getWidget",
                    {"path": {"widgetId": "widget-1"}},
                )

    result = asyncio.run(call_tool())

    assert result.is_error is False
    assert result.structured_content["policy"]["outcome"] == "allow"
    assert result.structured_content["execution"]["outcome"] == "executed"
    assert result.structured_content["execution"]["data"] == {
        "id": "widget-1",
        "status": "active",
    }
    assert len(upstream_requests) == 1
    assert upstream_requests[0].method == "GET"
    assert str(upstream_requests[0].url) == "http://localhost:9000/widgets/widget-1"
    assert len(provider.calls) == 1
    assert provider.calls[0]["connector_id"] == "synthetic"
    assert provider.calls[0]["operation_id"] == "getWidget"
    assert isinstance(provider.calls[0]["arguments"], ExecutionArguments)


def test_simulates_write_with_preview_and_zero_network(catalog: ConnectorCatalog) -> None:
    """Uma escrita aprovada no modo público vira prévia, nunca efeito externo."""

    provider = RecordingTrustedContextProvider(
        TrustedPolicySignals(
            principal=PolicyPrincipal(id="user-1", permissions=["action_high"]),
            direct_request=True,
        )
    )
    result, upstream_requests = _invoke_tool(
        catalog,
        name="synthetic.updateWidget",
        arguments={
            "path": {"widgetId": "widget-1"},
            "body": {
                "status": "inactive",
                "justification": "manutenção preventiva solicitada pela equipe",
            },
        },
        provider=provider,
    )

    assert result.is_error is False
    assert result.structured_content["policy"]["outcome"] == "simulate"
    assert result.structured_content["policy"]["confirmation_required_for_execute"] is True
    execution = result.structured_content["execution"]
    assert execution["outcome"] == "simulated"
    assert execution["attempts"] == 0
    assert execution["simulation"]["method"] == "PATCH"
    assert execution["simulation"]["path"] == "/widgets/widget-1"
    assert upstream_requests == []


def test_returns_policy_block_as_normal_structured_result(catalog: ConnectorCatalog) -> None:
    """Negação de permissão é decisão de negócio observável, não erro do protocolo MCP."""

    provider = RecordingTrustedContextProvider(
        TrustedPolicySignals(
            principal=PolicyPrincipal(id="user-1"),
            direct_request=True,
        )
    )
    result, upstream_requests = _invoke_tool(
        catalog,
        name="synthetic.updateWidget",
        arguments={
            "path": {"widgetId": "widget-1"},
            "body": {
                "status": "inactive",
                "justification": "manutenção preventiva solicitada pela equipe",
            },
        },
        provider=provider,
    )

    assert result.is_error is False
    assert result.structured_content["policy"]["outcome"] == "block"
    assert "PERMISSION_DENIED" in result.structured_content["policy"]["reason_codes"]
    assert result.structured_content["execution"] is None
    assert upstream_requests == []


def test_requires_confirmation_with_digest_without_network(catalog: ConnectorCatalog) -> None:
    """No modo execute, a confirmação pendente identifica exatamente a ação proposta."""

    provider = RecordingTrustedContextProvider(
        TrustedPolicySignals(
            principal=PolicyPrincipal(id="user-1", permissions=["action_high"]),
            direct_request=True,
        )
    )
    arguments = {
        "path": {"widgetId": "widget-1"},
        "body": {
            "status": "inactive",
            "justification": "manutenção preventiva solicitada pela equipe",
        },
    }
    result, upstream_requests = _invoke_tool(
        catalog,
        name="synthetic.updateWidget",
        arguments=arguments,
        provider=provider,
        execution_mode="execute",
    )

    policy = result.structured_content["policy"]
    assert result.is_error is False
    assert policy["outcome"] == "require_confirmation"
    assert policy["reason_codes"] == ["CONFIRMATION_REQUIRED"]
    assert len(policy["action_digest"]) == 64
    assert result.structured_content["execution"] is None
    assert upstream_requests == []

    provider.signals = TrustedPolicySignals(
        principal=PolicyPrincipal(id="user-1", permissions=["action_high"]),
        direct_request=True,
        confirmation=PolicyConfirmation(
            confirmed_by="user-1",
            action_digest=policy["action_digest"],
        ),
    )
    confirmed, confirmed_requests = _invoke_tool(
        catalog,
        name="synthetic.updateWidget",
        arguments=arguments,
        provider=provider,
        execution_mode="execute",
    )

    assert confirmed.is_error is False
    assert confirmed.structured_content["policy"]["outcome"] == "block"
    assert confirmed.structured_content["policy"]["reason_codes"] == ["REAL_WRITE_DISABLED"]
    assert confirmed.structured_content["execution"] is None
    assert confirmed_requests == []


def test_rejects_invalid_arguments_before_provider_and_network(catalog: ConnectorCatalog) -> None:
    """Valores fora do schema param no limite MCP sem tocar em sinais ou sistemas externos."""

    provider = RecordingTrustedContextProvider()
    result, upstream_requests = _invoke_tool(
        catalog,
        name="synthetic.getWidget",
        arguments={"path": {"widgetId": 42}, "private": "segredo-do-cliente"},
        provider=provider,
    )

    assert result.is_error is True
    assert result.structured_content["code"] == "MCP_TOOL_ARGUMENTS_INVALID"
    assert "segredo-do-cliente" not in result.content[0].text
    assert provider.calls == []
    assert upstream_requests == []


def test_blocks_mismatched_resource_scope_as_structured_result(catalog: ConnectorCatalog) -> None:
    """Escopo do principal, contexto e recurso precisa representar a mesma empresa."""

    provider = RecordingTrustedContextProvider(
        TrustedPolicySignals(
            principal=PolicyPrincipal(
                id="usr-001",
                permissions=["action_high"],
                scopes={"company_id": "company-a"},
            ),
            execution_context={"user_id": "usr-001", "company_id": "company-a"},
            resource_scopes={"company_id": "company-b"},
            direct_request=True,
        )
    )
    result, upstream_requests = _invoke_tool(
        catalog,
        name="tractian.updateAssetConfig",
        arguments={
            "path": {"assetId": "asset-1"},
            "body": {
                "justification": "alteração aprovada pela equipe de manutenção",
                "changes": {"criticality": "high"},
            },
        },
        provider=provider,
    )

    assert result.is_error is False
    assert result.structured_content["policy"]["outcome"] == "block"
    assert "SCOPE_MISMATCH" in result.structured_content["policy"]["reason_codes"]
    assert result.structured_content["execution"] is None
    assert upstream_requests == []


def test_does_not_expose_trusted_claim_values_in_simulation_result(
    catalog: ConnectorCatalog,
) -> None:
    """Claims autorizam a ação, mas o envelope contém apenas a decisão e a prévia necessárias."""

    provider = RecordingTrustedContextProvider(
        TrustedPolicySignals(
            principal=PolicyPrincipal(
                id="private-user",
                permissions=["action_high"],
                scopes={"company_id": "private-company"},
            ),
            execution_context={
                "user_id": "private-user",
                "company_id": "private-company",
            },
            resource_scopes={"company_id": "private-company"},
            direct_request=True,
        )
    )
    result, upstream_requests = _invoke_tool(
        catalog,
        name="tractian.updateAssetConfig",
        arguments={
            "path": {"assetId": "asset-1"},
            "body": {
                "justification": "alteração aprovada pela equipe de manutenção",
                "changes": {"criticality": "high"},
            },
        },
        provider=provider,
    )

    serialized = result.content[0].text
    assert result.is_error is False
    assert result.structured_content["policy"]["outcome"] == "simulate"
    assert "private-user" not in serialized
    assert "private-company" not in serialized
    assert upstream_requests == []


def test_returns_sanitized_error_for_unknown_tool(catalog: ConnectorCatalog) -> None:
    """Tool desconhecida é erro do adaptador e não chega ao provider nem à rede."""

    provider = RecordingTrustedContextProvider()
    result, upstream_requests = _invoke_tool(
        catalog,
        name="synthetic.doesNotExist",
        arguments={},
        provider=provider,
    )

    assert result.is_error is True
    assert result.structured_content == {
        "code": "MCP_TOOL_NOT_FOUND",
        "message": "A tool solicitada não existe.",
    }
    assert provider.calls == []
    assert upstream_requests == []


@pytest.mark.parametrize("provider", [None, FailingTrustedContextProvider()])
def test_returns_sanitized_error_when_trusted_context_is_unavailable(
    catalog: ConnectorCatalog,
    provider: Any,
) -> None:
    """Ausência ou falha do provider permanece redigida e impede todo acesso HTTP."""

    result, upstream_requests = _invoke_tool(
        catalog,
        name="synthetic.getWidget",
        arguments={"path": {"widgetId": "widget-1"}},
        provider=provider,
    )

    assert result.is_error is True
    assert result.structured_content["code"] == "TRUSTED_CONTEXT_UNAVAILABLE"
    assert "token-super-secreto" not in result.content[0].text
    assert upstream_requests == []


def test_keeps_upstream_failure_distinct_from_mcp_and_policy_errors(
    catalog: ConnectorCatalog,
) -> None:
    """Falha 5xx autorizada continua no envelope do executor e respeita retry idempotente."""

    result, upstream_requests = _invoke_tool(
        catalog,
        name="synthetic.getWidget",
        arguments={"path": {"widgetId": "widget-1"}},
        provider=RecordingTrustedContextProvider(),
        upstream=lambda _: httpx.Response(503, json={"message": "temporarily unavailable"}),
    )

    assert result.is_error is False
    assert result.structured_content["policy"]["outcome"] == "allow"
    execution = result.structured_content["execution"]
    assert execution["outcome"] == "failed"
    assert execution["status_code"] == 503
    assert execution["error"]["code"] == "UPSTREAM_HTTP_ERROR"
    assert execution["attempts"] == 3
    assert len(upstream_requests) == 3


def test_redacts_unexpected_internal_failure(catalog: ConnectorCatalog) -> None:
    """Exceção inesperada na fronteira HTTP não devolve segredo nem stack trace pelo MCP."""

    def broken_upstream(_: httpx.Request) -> httpx.Response:
        raise RuntimeError("segredo-interno-do-transporte")

    result, upstream_requests = _invoke_tool(
        catalog,
        name="synthetic.getWidget",
        arguments={"path": {"widgetId": "widget-1"}},
        provider=RecordingTrustedContextProvider(),
        upstream=broken_upstream,
    )

    assert result.is_error is True
    assert result.structured_content["code"] == "MCP_TOOL_INTERNAL_ERROR"
    assert "segredo-interno-do-transporte" not in result.content[0].text
    assert "Traceback" not in result.content[0].text
    assert len(upstream_requests) == 1


def test_generates_self_contained_tool_for_new_connector_without_core_changes(
    tmp_path: Path,
) -> None:
    """OpenAPI + profile bastam para criar uma tool, inclusive quando o schema usa $ref."""

    catalog = _temporary_connector_catalog(tmp_path)
    server = create_mcp_server(
        catalog,
        _guarded_executor(catalog),
        StaticTrustedContextProvider(),
    )

    tools = _list_tools(server)
    assert [tool.name for tool in tools] == ["extension.getThing"]
    schema = tools[0].input_schema
    assert schema["properties"]["path"]["properties"]["thingId"] == {
        "$ref": "#/$defs/components__schemas__Identifier"
    }
    assert schema["$defs"] == {
        "components__schemas__Identifier": {
            "type": "string",
            "pattern": "^thing-[0-9]+$",
        }
    }
    assert "#/components/" not in str(schema)
    validator = validator_for(schema)
    validator.check_schema(schema)
    assert list(validator(schema).iter_errors({"path": {"thingId": "thing-42"}})) == []


def test_omits_disabled_operation_from_mcp_discovery(tmp_path: Path) -> None:
    """Descoberta no OpenAPI não concede acesso quando o profile mantém a operação fechada."""

    catalog = _temporary_connector_catalog(tmp_path, enabled=False)
    server = create_mcp_server(
        catalog,
        _guarded_executor(catalog),
        StaticTrustedContextProvider(),
    )

    assert _list_tools(server) == []


def test_rejects_invalid_tool_name_during_server_construction(tmp_path: Path) -> None:
    """operationId incompatível com o protocolo falha no startup, antes de qualquer cliente."""

    catalog = _temporary_connector_catalog(tmp_path, operation_id="invalid tool name")

    with pytest.raises(McpServerConfigurationError, match="nome de tool MCP inválido"):
        create_mcp_server(
            catalog,
            _guarded_executor(catalog),
            StaticTrustedContextProvider(),
        )


def test_rejects_unsupported_argument_location_during_server_construction(
    tmp_path: Path,
) -> None:
    """Parâmetro cookie não pode desaparecer silenciosamente do contrato entregue ao agente."""

    catalog = _temporary_connector_catalog(tmp_path, include_cookie=True)

    with pytest.raises(McpServerConfigurationError, match="local de parâmetro não suportado"):
        create_mcp_server(
            catalog,
            _guarded_executor(catalog),
            StaticTrustedContextProvider(),
        )


def test_rejects_tool_name_collision_during_server_construction(
    catalog: ConnectorCatalog,
) -> None:
    """Mesmo uma fonte de catálogo defeituosa não pode sobrescrever uma tool silenciosamente."""

    class RepeatedConnectorCatalog:
        def list(self) -> list[Any]:
            connectors = catalog.list()
            return [*connectors, connectors[0]]

        def get(self, connector_id: str) -> Any:
            return catalog.get(connector_id)

        def resolve_operation(self, connector_id: str, operation_id: str) -> Any:
            return catalog.resolve_operation(connector_id, operation_id)

    with pytest.raises(McpServerConfigurationError, match="colisão de nome de tool MCP"):
        create_mcp_server(  # type: ignore[arg-type]
            RepeatedConnectorCatalog(),
            _guarded_executor(catalog),
            StaticTrustedContextProvider(),
        )
