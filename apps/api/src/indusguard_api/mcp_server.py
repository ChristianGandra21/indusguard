"""Adapta operações OpenAPI protegidas para o protocolo MCP.

O servidor criado aqui é um objeto interno: ele não abre porta, não cria subprocesso e não
expõe uma rota FastAPI. O futuro host do agente poderá conectá-lo em memória e descobrir as
mesmas operações que já foram validadas pelo catálogo.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from json import dumps
from typing import Any, Protocol

from jsonschema.validators import validator_for
from mcp.server import Server
from mcp_types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    TextContent,
    Tool,
    ToolAnnotations,
)
from pydantic import BaseModel, ConfigDict, Field

from indusguard_api.connectors import ConnectorCatalog, ResolvedOperation
from indusguard_api.policy import GuardedExecutor
from indusguard_api.schemas import (
    AccessMode,
    ExecutionArguments,
    GuardedExecutionResult,
    OperationExecutionRequest,
    PolicyConfirmation,
    PolicyEvaluationRequest,
    PolicyPrincipal,
    ScopeValue,
)

_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_ARGUMENT_LOCATIONS = (("path", "path"), ("query", "query"), ("header", "headers"))


class McpServerConfigurationError(ValueError):
    """Indica que um catálogo não pode ser publicado de forma segura como tools MCP."""


class TrustedPolicySignals(BaseModel):
    """Sinais produzidos pelo runtime autenticado, fora do controle do modelo.

    Esses campos nunca entram no ``inputSchema`` das tools. O provider concreto será responsável
    por descobrir identidade, autorizações e escopos antes de cada chamada.
    """

    model_config = ConfigDict(extra="forbid")

    principal: PolicyPrincipal | None = None
    execution_context: dict[str, Any] = Field(default_factory=dict)
    resource_scopes: dict[str, ScopeValue] = Field(default_factory=dict)
    direct_request: bool = False
    confirmation: PolicyConfirmation | None = None


class TrustedPolicyContextProvider(Protocol):
    """Fronteira assíncrona para obter sinais que uma tool não pode autodeclarar."""

    async def resolve(
        self,
        *,
        connector_id: str,
        operation_id: str,
        arguments: ExecutionArguments,
    ) -> TrustedPolicySignals:
        """Resolve os sinais confiáveis associados à chamada já validada."""


@dataclass(frozen=True)
class _RegisteredTool:
    """Associação interna que impede o cliente de escolher conector ou operação."""

    connector_id: str
    operation_id: str
    tool: Tool


class _SchemaBundler:
    """Reescreve referências OpenAPI locais para ``$defs`` do schema MCP.

    Um ``$ref`` OpenAPI aponta para a raiz do documento completo. Essa raiz não será enviada ao
    cliente MCP; por isso copiamos somente as definições realmente usadas e alteramos os
    apontadores para que o resultado continue autocontido.
    """

    def __init__(self, reference_document: Mapping[str, Any]) -> None:
        self._reference_document = reference_document
        self.definitions: dict[str, Any] = {}

    def rewrite(self, value: Any) -> Any:
        """Copia um fragmento de schema e converte referências em qualquer profundidade."""

        if isinstance(value, list):
            return [self.rewrite(item) for item in value]
        if not isinstance(value, Mapping):
            return deepcopy(value)

        reference = value.get("$ref")
        rewritten: dict[str, Any] = {}
        if reference is not None:
            if not isinstance(reference, str) or not reference.startswith("#/"):
                raise McpServerConfigurationError("schema MCP contém $ref não local")
            definition_name = self._definition_name(reference)
            # O placeholder interrompe ciclos como Node -> children -> Node.
            if definition_name not in self.definitions:
                self.definitions[definition_name] = {}
                self.definitions[definition_name] = self.rewrite(self._resolve(reference))
            rewritten["$ref"] = f"#/$defs/{definition_name}"

        for key, child in value.items():
            if key != "$ref":
                rewritten[str(key)] = self.rewrite(child)
        return rewritten

    def _resolve(self, reference: str) -> Any:
        """Resolve JSON Pointer local sem acessar arquivos ou rede."""

        current: Any = self._reference_document
        for raw_token in reference[2:].split("/"):
            token = raw_token.replace("~1", "/").replace("~0", "~")
            if not isinstance(current, Mapping) or token not in current:
                raise McpServerConfigurationError(f"$ref local não resolvido: '{reference}'")
            current = current[token]
        return current

    @staticmethod
    def _definition_name(reference: str) -> str:
        """Produz chave legível e determinística sem caracteres especiais de JSON Pointer."""

        tokens = [token.replace("~1", "_").replace("~0", "~") for token in reference[2:].split("/")]
        return "__".join(tokens)


def _json_body_schema(resolved: ResolvedOperation) -> tuple[Any, bool] | None:
    """Seleciona o schema do request body JSON suportado pelo executor atual."""

    request_body = resolved.request_body
    if request_body is None:
        return None
    content = request_body.get("content", {})
    if not isinstance(content, Mapping):
        raise McpServerConfigurationError("requestBody.content precisa ser um objeto")
    media = content.get("application/json")
    if media is None:
        json_media = [value for key, value in content.items() if str(key).endswith("+json")]
        media = json_media[0] if len(json_media) == 1 else None
    if not isinstance(media, Mapping) or "schema" not in media:
        raise McpServerConfigurationError("requestBody JSON precisa declarar schema")
    return media["schema"], bool(request_body.get("required", False))


def _input_schema(resolved: ResolvedOperation) -> dict[str, Any]:
    """Converte os argumentos posicionais da operação em um contrato MCP fechado."""

    bundler = _SchemaBundler(resolved.reference_document)
    top_properties: dict[str, Any] = {}
    top_required: list[str] = []
    supported_locations = {location for location, _ in _ARGUMENT_LOCATIONS}
    unsupported_locations = {
        str(parameter.get("in"))
        for parameter in resolved.parameters
        if parameter.get("in") not in supported_locations
    }
    if unsupported_locations:
        locations = ", ".join(sorted(unsupported_locations))
        raise McpServerConfigurationError(
            f"local de parâmetro não suportado em '{resolved.operation.operation_id}': {locations}"
        )

    for openapi_location, argument_group in _ARGUMENT_LOCATIONS:
        parameters = [
            parameter
            for parameter in resolved.parameters
            if parameter.get("in") == openapi_location
        ]
        if not parameters:
            continue

        group_properties: dict[str, Any] = {}
        group_required: list[str] = []
        for parameter in parameters:
            name = parameter.get("name")
            schema = parameter.get("schema")
            if not isinstance(name, str) or not name or schema is None:
                raise McpServerConfigurationError(
                    f"parâmetro inválido em '{resolved.operation.operation_id}'"
                )
            parameter_schema = bundler.rewrite(schema)
            if isinstance(parameter_schema, dict) and parameter.get("description"):
                parameter_schema.setdefault("description", parameter["description"])
            group_properties[name] = parameter_schema
            if parameter.get("required") is True:
                group_required.append(name)

        group_schema: dict[str, Any] = {
            "type": "object",
            "properties": group_properties,
        }
        if group_required:
            group_schema["required"] = group_required
            top_required.append(argument_group)
        group_schema["additionalProperties"] = False
        top_properties[argument_group] = group_schema

    body = _json_body_schema(resolved)
    if body is not None:
        body_schema, body_required = body
        top_properties["body"] = bundler.rewrite(body_schema)
        if body_required:
            top_required.append("body")

    schema: dict[str, Any] = {
        "type": "object",
        "properties": top_properties,
    }
    if top_required:
        schema["required"] = top_required
    schema["additionalProperties"] = False
    if bundler.definitions:
        schema["$defs"] = bundler.definitions

    try:
        validator_for(schema).check_schema(schema)
    except Exception as exc:
        raise McpServerConfigurationError(
            f"schema MCP inválido para '{resolved.operation.operation_id}'"
        ) from exc
    return schema


def _snapshot_tools(catalog: ConnectorCatalog) -> dict[str, _RegisteredTool]:
    """Cria uma fotografia estável das operações habilitadas no startup do servidor."""

    registry: dict[str, _RegisteredTool] = {}
    output_schema = GuardedExecutionResult.model_json_schema()

    for connector in catalog.list():
        details = catalog.get(connector.id)
        if details is None:  # O catálogo carregado não deveria mudar durante o snapshot.
            raise McpServerConfigurationError(
                f"conector '{connector.id}' desapareceu durante a construção do MCP"
            )
        for operation in details.operations:
            if not operation.enabled:
                continue
            resolved = catalog.resolve_operation(connector.id, operation.operation_id)
            if resolved is None:
                raise McpServerConfigurationError(
                    f"operação '{operation.operation_id}' desapareceu durante a construção do MCP"
                )
            name = f"{connector.id}.{operation.operation_id}"
            if not _TOOL_NAME_PATTERN.fullmatch(name):
                raise McpServerConfigurationError(f"nome de tool MCP inválido: '{name}'")
            if name in registry:
                raise McpServerConfigurationError(f"colisão de nome de tool MCP: '{name}'")
            tool = Tool(
                name=name,
                title=operation.summary or operation.operation_id,
                description=operation.summary,
                input_schema=_input_schema(resolved),
                output_schema=output_schema,
                annotations=ToolAnnotations(
                    read_only_hint=operation.access is AccessMode.READ,
                    destructive_hint=operation.access is AccessMode.WRITE,
                    idempotent_hint=operation.idempotent,
                    open_world_hint=True,
                ),
            )
            registry[name] = _RegisteredTool(
                connector_id=connector.id,
                operation_id=operation.operation_id,
                tool=tool,
            )

    return dict(sorted(registry.items()))


def _error_result(code: str, message: str) -> CallToolResult:
    """Cria erro MCP estável sem argumentos recebidos, credenciais ou stack trace."""

    payload = {"code": code, "message": message}
    return CallToolResult(
        content=[TextContent(text=dumps(payload, ensure_ascii=False, sort_keys=True))],
        structured_content=payload,
        is_error=True,
    )


def _valid_arguments(schema: Mapping[str, Any], arguments: Any) -> bool:
    """Valida o contrato público antes de acessar qualquer fonte confiável."""

    try:
        validator = validator_for(schema)
        validator.check_schema(schema)
        return not any(validator(schema).iter_errors(arguments))
    except Exception:
        # Um schema impossível deveria ter falhado no startup. Ainda assim, a fronteira de
        # chamada permanece fechada caso uma implementação de validator se comporte diferente.
        return False


def create_mcp_server(
    catalog: ConnectorCatalog,
    guarded_executor: GuardedExecutor,
    context_provider: TrustedPolicyContextProvider | None,
) -> Server[None]:
    """Cria o servidor MCP interno a partir de dependências explicitamente confiáveis.

    Não existe provider default: quem hospedar o servidor precisa escolher conscientemente de
    onde virão identidade, permissões, escopos e confirmações.
    """

    registry = _snapshot_tools(catalog)
    tools = [registered.tool for registered in registry.values()]

    async def list_tools(_context: object, _params: object) -> ListToolsResult:
        return ListToolsResult(tools=tools)

    async def call_tool(_context: object, params: CallToolRequestParams) -> CallToolResult:
        registered = registry.get(params.name)
        if registered is None:
            return _error_result("MCP_TOOL_NOT_FOUND", "A tool solicitada não existe.")

        raw_arguments = params.arguments if params.arguments is not None else {}
        if not _valid_arguments(registered.tool.input_schema, raw_arguments):
            return _error_result(
                "MCP_TOOL_ARGUMENTS_INVALID",
                "Os argumentos não atendem ao schema publicado pela tool.",
            )

        # A validação JSON Schema ocorre primeiro. O modelo Pydantic preserva a diferença entre
        # body ausente e body explicitamente nulo, usada pelo executor HTTP.
        try:
            arguments = ExecutionArguments.model_validate(raw_arguments)
        except Exception:
            return _error_result(
                "MCP_TOOL_ARGUMENTS_INVALID",
                "Os argumentos não atendem ao contrato interno da tool.",
            )

        if context_provider is None:
            return _error_result(
                "TRUSTED_CONTEXT_UNAVAILABLE",
                "O contexto confiável não está disponível para esta chamada.",
            )
        try:
            raw_signals = await context_provider.resolve(
                connector_id=registered.connector_id,
                operation_id=registered.operation_id,
                arguments=arguments,
            )
            signals = TrustedPolicySignals.model_validate(raw_signals)
        except Exception:
            return _error_result(
                "TRUSTED_CONTEXT_UNAVAILABLE",
                "O contexto confiável não está disponível para esta chamada.",
            )

        request = PolicyEvaluationRequest(
            execution=OperationExecutionRequest(
                connector_id=registered.connector_id,
                operation_id=registered.operation_id,
                arguments=arguments,
                context=signals.execution_context,
            ),
            principal=signals.principal,
            resource_scopes=signals.resource_scopes,
            direct_request=signals.direct_request,
            confirmation=signals.confirmation,
        )
        try:
            result = await guarded_executor.execute(request)
        except Exception:
            return _error_result(
                "MCP_TOOL_INTERNAL_ERROR",
                "A chamada não pôde ser concluída pelo runtime protegido.",
            )

        payload = result.model_dump(mode="json")
        return CallToolResult(
            content=[TextContent(text=dumps(payload, ensure_ascii=False, sort_keys=True))],
            structured_content=payload,
        )

    return Server(
        "indusguard",
        version="0.1.0",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
