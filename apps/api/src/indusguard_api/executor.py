"""Executor HTTP genérico construído sobre o catálogo validado de conectores.

O segundo corte vertical executa operações GET com parâmetros de path, query e header, além de
autenticação ``none`` ou ``context_header``. Request bodies também são validados para preparar a
futura simulação de escritas, mas métodos mutáveis continuam bloqueados antes da rede.

As fronteiras de segurança são deliberadamente determinísticas:

* o request recebe ``connector_id`` e ``operation_id``, nunca uma URL arbitrária;
* somente operações habilitadas pelo profile chegam à preparação HTTP;
* argumentos são comparados e validados contra o OpenAPI;
* autenticação derivada do contexto não pode ser sobrescrita pelos argumentos;
* a URL-base vem do ambiente e precisa coincidir com a allowlist;
* sucesso, bloqueio e falha usam o mesmo envelope, independentemente da API.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Final
from urllib.parse import quote

import httpx
from jsonschema import SchemaError
from jsonschema.validators import validator_for

from indusguard_api.connectors import ConnectorCatalog, ResolvedOperation
from indusguard_api.schemas import (
    ExecutionArguments,
    ExecutionErrorDetails,
    ExecutionOutcome,
    OperationExecutionRequest,
    OperationExecutionResult,
)

PATH_PLACEHOLDER: Final = re.compile(r"\{([^{}]+)\}")
HEADER_NAME: Final = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
NO_BODY: Final = object()


@dataclass(frozen=True)
class PreparedRequest:
    """Partes HTTP validadas, ainda sem abrir conexão com o upstream."""

    path: str
    query: tuple[tuple[str, str], ...]
    headers: dict[str, str]
    body: Any


class ExecutionValidationError(ValueError):
    """Erro determinístico detectado antes que uma chamada HTTP seja permitida."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _normalize_base_url(value: str) -> str:
    """Normaliza uma URL-base e rejeita partes que não pertencem a um destino fixo.

    Query, fragmento e credenciais embutidas poderiam alterar o significado da URL ou vazar
    segredos. A autenticação deve vir exclusivamente do mecanismo declarado no profile.
    """

    try:
        url = httpx.URL(value)
    except (httpx.InvalidURL, TypeError, ValueError) as exc:
        raise ExecutionValidationError(
            "INVALID_BASE_URL",
            "a variável de URL-base não contém uma URL válida",
        ) from exc

    if url.scheme not in {"http", "https"} or not url.host or not url.is_absolute_url:
        raise ExecutionValidationError(
            "INVALID_BASE_URL",
            "a URL-base precisa ser HTTP ou HTTPS e possuir um host",
        )
    if url.userinfo or url.query or url.fragment:
        raise ExecutionValidationError(
            "INVALID_BASE_URL",
            "a URL-base não pode conter credenciais, query ou fragmento",
        )

    return str(url).rstrip("/")


def _serialize_primitive(value: Any, *, location: str) -> str:
    """Serializa um valor JSON primitivo sem aceitar estruturas ambíguas."""

    if value is None or isinstance(value, (dict, list)):
        raise ExecutionValidationError(
            f"UNSUPPORTED_{location.upper()}_SERIALIZATION",
            f"{location} aceita somente valores primitivos neste incremento",
        )
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _validate_schema(
    value: Any,
    schema: Mapping[str, Any],
    reference_document: Mapping[str, Any],
    *,
    invalid_code: str,
    label: str,
) -> None:
    """Valida um valor usando o schema e a raiz OpenAPI para resolver referências locais.

    O schema fica em ``allOf`` e o documento OpenAPI permanece como raiz. Dessa forma, uma
    referência como ``#/components/schemas/ActionRequest`` continua apontando para o local
    correto sem buscar arquivo ou host externo.
    """

    validation_document = deepcopy(dict(reference_document))
    validation_document["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    validation_document["allOf"] = [deepcopy(dict(schema))]
    try:
        validator_class = validator_for(validation_document)
        validator_class.check_schema(validation_document)
        errors = sorted(
            validator_class(validation_document).iter_errors(value),
            key=lambda error: tuple(str(part) for part in error.path),
        )
    except SchemaError as exc:
        raise ExecutionValidationError(
            "INVALID_OPERATION_CONTRACT",
            f"o schema de {label} é inválido",
        ) from exc
    if errors:
        # A mensagem do jsonschema pode reproduzir o valor rejeitado. Não a devolvemos porque um
        # argumento inválido ainda pode ser segredo ou dado pessoal.
        raise ExecutionValidationError(
            invalid_code,
            f"{label} não corresponde ao schema OpenAPI",
        )


def _parameter_definitions(
    resolved: ResolvedOperation,
    location: str,
    *,
    case_insensitive: bool = False,
) -> dict[str, dict[str, Any]]:
    """Indexa parâmetros já resolvidos e detecta colisões no contrato."""

    definitions: dict[str, dict[str, Any]] = {}
    for parameter in resolved.parameters:
        if parameter.get("in") != location:
            continue
        name = str(parameter["name"])
        key = name.lower() if case_insensitive else name
        if key in definitions:
            raise ExecutionValidationError(
                "INVALID_OPERATION_CONTRACT",
                f"parâmetro duplicado em {location}: {name}",
            )
        definitions[key] = parameter
    return definitions


def _check_argument_names(
    definitions: Mapping[str, Mapping[str, Any]],
    arguments: Mapping[str, Any],
    *,
    location: str,
) -> None:
    """Rejeita argumentos ausentes ou desconhecidos antes de validar seus valores."""

    provided = set(arguments)
    required = {
        name for name, parameter in definitions.items() if bool(parameter.get("required", False))
    }
    missing = required - provided
    unexpected = provided - set(definitions)
    if missing:
        names = ", ".join(sorted(missing))
        raise ExecutionValidationError(
            f"MISSING_{location.upper()}_ARGUMENT",
            f"faltam argumentos obrigatórios de {location}: {names}",
        )
    if unexpected:
        names = ", ".join(sorted(unexpected))
        raise ExecutionValidationError(
            f"UNEXPECTED_{location.upper()}_ARGUMENT",
            f"foram enviados argumentos desconhecidos de {location}: {names}",
        )


def _render_path(resolved: ResolvedOperation, arguments: Mapping[str, Any]) -> str:
    """Valida parâmetros de path pelo OpenAPI e preenche o template da operação."""

    template = resolved.operation.path
    placeholders = set(PATH_PLACEHOLDER.findall(template))
    definitions = _parameter_definitions(resolved, "path")

    if placeholders != set(definitions):
        raise ExecutionValidationError(
            "INVALID_OPERATION_CONTRACT",
            "os placeholders do path não correspondem aos parâmetros declarados no OpenAPI",
        )
    _check_argument_names(definitions, arguments, location="path")

    rendered = template
    for name in sorted(placeholders):
        value = arguments[name]
        _validate_schema(
            value,
            definitions[name].get("schema", {}),
            resolved.reference_document,
            invalid_code="INVALID_PATH_ARGUMENT",
            label=f"argumento de path '{name}'",
        )
        encoded = quote(_serialize_primitive(value, location="path"), safe="")
        rendered = rendered.replace(f"{{{name}}}", encoded)

    return rendered


def _serialize_query_parameter(
    name: str,
    value: Any,
    parameter: Mapping[str, Any],
) -> list[tuple[str, str]]:
    """Implementa o subconjunto previsível do estilo OpenAPI ``form``."""

    style = parameter.get("style", "form")
    explode = parameter.get("explode", True)
    if style != "form":
        raise ExecutionValidationError(
            "UNSUPPORTED_QUERY_STYLE",
            f"parâmetro de query '{name}' usa um estilo ainda não suportado",
        )
    if isinstance(value, dict):
        raise ExecutionValidationError(
            "UNSUPPORTED_QUERY_SERIALIZATION",
            f"parâmetro de query '{name}' não aceita objeto neste incremento",
        )
    if isinstance(value, list):
        serialized = [_serialize_primitive(item, location="query") for item in value]
        if explode:
            return [(name, item) for item in serialized]
        return [(name, ",".join(serialized))]
    return [(name, _serialize_primitive(value, location="query"))]


def _build_query(
    resolved: ResolvedOperation,
    arguments: Mapping[str, Any],
) -> tuple[tuple[str, str], ...]:
    """Valida e serializa parâmetros de query sem concatenar strings manualmente."""

    definitions = _parameter_definitions(resolved, "query")
    _check_argument_names(definitions, arguments, location="query")
    serialized: list[tuple[str, str]] = []
    for name, value in arguments.items():
        parameter = definitions[name]
        _validate_schema(
            value,
            parameter.get("schema", {}),
            resolved.reference_document,
            invalid_code="INVALID_QUERY_ARGUMENT",
            label=f"argumento de query '{name}'",
        )
        serialized.extend(_serialize_query_parameter(name, value, parameter))
    return tuple(serialized)


def _normalize_header_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Normaliza nomes de header e impede duas grafias para a mesma chave."""

    normalized: dict[str, Any] = {}
    for name, value in arguments.items():
        key = name.lower()
        if key in normalized:
            raise ExecutionValidationError(
                "DUPLICATE_HEADER_ARGUMENT",
                f"header '{name}' foi enviado mais de uma vez",
            )
        normalized[key] = value
    return normalized


def _serialize_header_parameter(
    name: str,
    value: Any,
    parameter: Mapping[str, Any],
) -> str:
    """Serializa primitive/array no estilo ``simple`` usado por headers OpenAPI."""

    if parameter.get("style", "simple") != "simple":
        raise ExecutionValidationError(
            "UNSUPPORTED_HEADER_STYLE",
            f"header '{name}' usa um estilo ainda não suportado",
        )
    if isinstance(value, dict):
        raise ExecutionValidationError(
            "UNSUPPORTED_HEADER_SERIALIZATION",
            f"header '{name}' não aceita objeto neste incremento",
        )
    if isinstance(value, list):
        serialized = ",".join(_serialize_primitive(item, location="header") for item in value)
    else:
        serialized = _serialize_primitive(value, location="header")
    if "\r" in serialized or "\n" in serialized:
        raise ExecutionValidationError(
            "INVALID_HEADER_ARGUMENT",
            f"header '{name}' contém quebra de linha",
        )
    return serialized


def _build_headers(
    resolved: ResolvedOperation,
    arguments: Mapping[str, Any],
) -> dict[str, str]:
    """Valida headers declarados na operação e preserva seus nomes canônicos."""

    definitions = _parameter_definitions(resolved, "header", case_insensitive=True)
    normalized = _normalize_header_arguments(arguments)
    _check_argument_names(definitions, normalized, location="header")
    headers: dict[str, str] = {}
    for key, value in normalized.items():
        parameter = definitions[key]
        canonical_name = str(parameter["name"])
        if not HEADER_NAME.fullmatch(canonical_name):
            raise ExecutionValidationError(
                "INVALID_OPERATION_CONTRACT",
                f"nome de header inválido no OpenAPI: {canonical_name}",
            )
        _validate_schema(
            value,
            parameter.get("schema", {}),
            resolved.reference_document,
            invalid_code="INVALID_HEADER_ARGUMENT",
            label=f"header '{canonical_name}'",
        )
        headers[canonical_name] = _serialize_header_parameter(canonical_name, value, parameter)
    return headers


def _build_auth_headers(
    resolved: ResolvedOperation,
    context: Mapping[str, Any],
    provided_headers: Mapping[str, Any],
) -> dict[str, str]:
    """Deriva autenticação do contexto sem aceitar credencial enviada como argumento."""

    auth = resolved.profile.auth
    if auth.type == "none":
        return {}
    if auth.type != "context_header":
        raise ExecutionValidationError(
            "AUTH_NOT_SUPPORTED",
            f"autenticação '{auth.type}' ainda não é suportada",
        )

    header_name = auth.name or ""
    context_field = auth.context_field or ""
    if not HEADER_NAME.fullmatch(header_name):
        raise ExecutionValidationError(
            "INVALID_OPERATION_CONTRACT",
            "o profile contém um nome inválido para o header de autenticação",
        )
    if header_name.lower() in {name.lower() for name in provided_headers}:
        raise ExecutionValidationError(
            "RESERVED_AUTH_HEADER",
            f"o header de autenticação '{header_name}' só pode vir do contexto",
        )
    if context_field not in context:
        raise ExecutionValidationError(
            "AUTH_CONTEXT_MISSING",
            f"o contexto não contém o campo obrigatório '{context_field}'",
        )

    serialized = _serialize_primitive(context[context_field], location="auth_header")
    if "\r" in serialized or "\n" in serialized:
        raise ExecutionValidationError(
            "INVALID_AUTH_CONTEXT",
            f"o campo de contexto '{context_field}' contém um valor inválido",
        )
    return {header_name: serialized}


def _build_body(resolved: ResolvedOperation, arguments: ExecutionArguments) -> Any:
    """Valida request body JSON e diferencia ausência de ``null`` explícito."""

    supplied = "body" in arguments.model_fields_set
    request_body = resolved.request_body
    if request_body is None:
        if supplied:
            raise ExecutionValidationError(
                "UNEXPECTED_REQUEST_BODY",
                "a operação não declara request body no OpenAPI",
            )
        return NO_BODY
    if not supplied:
        if bool(request_body.get("required", False)):
            raise ExecutionValidationError(
                "MISSING_REQUEST_BODY",
                "a operação exige um request body",
            )
        return NO_BODY

    content = request_body.get("content", {})
    media_type = next(
        (name for name in content if name == "application/json" or str(name).endswith("+json")),
        None,
    )
    if media_type is None:
        raise ExecutionValidationError(
            "INVALID_OPERATION_CONTRACT",
            "a operação não declara um request body JSON",
        )
    schema = content[media_type].get("schema", {})
    _validate_schema(
        arguments.body,
        schema,
        resolved.reference_document,
        invalid_code="INVALID_REQUEST_BODY",
        label="request body",
    )
    return arguments.body


class HttpExecutor:
    """Executa operações conhecidas usando dependências injetáveis e defaults seguros."""

    def __init__(
        self,
        catalog: ConnectorCatalog,
        *,
        environment: Mapping[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._catalog = catalog
        self._environment = os.environ if environment is None else environment
        self._client = client

    async def execute(self, request: OperationExecutionRequest) -> OperationExecutionResult:
        """Valida, prepara, executa e normaliza uma operação do catálogo."""

        started_at = perf_counter()
        connector = self._catalog.get(request.connector_id)
        if connector is None:
            return self._blocked(
                request,
                started_at,
                "CONNECTOR_NOT_FOUND",
                f"conector '{request.connector_id}' não encontrado",
            )

        resolved = self._catalog.resolve_operation(request.connector_id, request.operation_id)
        if resolved is None:
            return self._blocked(
                request,
                started_at,
                "OPERATION_NOT_FOUND",
                f"operação '{request.operation_id}' não encontrada no conector",
            )
        if not resolved.operation.enabled:
            return self._blocked(
                request,
                started_at,
                "OPERATION_DISABLED",
                f"operação '{request.operation_id}' está desabilitada pelo profile",
            )

        try:
            prepared = self._prepare_request(resolved, request)
            base_url = self._resolve_base_url(resolved)
        except ExecutionValidationError as exc:
            return self._blocked(request, started_at, exc.code, str(exc))

        # Preparar e validar o body de uma escrita já é útil para o próximo incremento, mas apenas
        # GET possui autorização de transporte nesta etapa.
        if resolved.operation.method != "GET":
            return self._blocked(
                request,
                started_at,
                "METHOD_NOT_SUPPORTED",
                "este incremento executa somente GET; escritas continuam bloqueadas",
            )

        url = f"{base_url}{prepared.path}"
        try:
            response = await self._send_request(
                method=resolved.operation.method,
                url=url,
                query=prepared.query,
                headers=prepared.headers,
                body=prepared.body,
                timeout_seconds=resolved.operation.timeout_seconds,
            )
        except httpx.TimeoutException:
            return self._failed(
                request,
                started_at,
                "UPSTREAM_TIMEOUT",
                "a API conectada excedeu o timeout configurado",
                retryable=True,
            )
        except httpx.RequestError:
            return self._failed(
                request,
                started_at,
                "UPSTREAM_CONNECTION_ERROR",
                "não foi possível conectar à API configurada",
                retryable=True,
            )

        try:
            data = response.json() if response.content else None
        except ValueError:
            return self._failed(
                request,
                started_at,
                "INVALID_JSON_RESPONSE",
                "a API conectada retornou um corpo que não é JSON válido",
                status_code=response.status_code,
            )

        if not 200 <= response.status_code < 300:
            return OperationExecutionResult(
                connector_id=request.connector_id,
                operation_id=request.operation_id,
                outcome=ExecutionOutcome.FAILED,
                status_code=response.status_code,
                data=data,
                error=ExecutionErrorDetails(
                    code="UPSTREAM_HTTP_ERROR",
                    message=f"a API conectada respondeu com HTTP {response.status_code}",
                    retryable=response.status_code == 429 or response.status_code >= 500,
                ),
                latency_ms=self._elapsed_ms(started_at),
            )

        return OperationExecutionResult(
            connector_id=request.connector_id,
            operation_id=request.operation_id,
            outcome=ExecutionOutcome.EXECUTED,
            status_code=response.status_code,
            data=data,
            latency_ms=self._elapsed_ms(started_at),
        )

    def _prepare_request(
        self,
        resolved: ResolvedOperation,
        request: OperationExecutionRequest,
    ) -> PreparedRequest:
        """Compila os argumentos somente depois de todas as validações locais."""

        path = _render_path(resolved, request.arguments.path)
        query = _build_query(resolved, request.arguments.query)
        auth_headers = _build_auth_headers(
            resolved,
            request.context,
            request.arguments.headers,
        )
        headers = _build_headers(resolved, request.arguments.headers)
        headers.update(auth_headers)
        body = _build_body(resolved, request.arguments)
        return PreparedRequest(path=path, query=query, headers=headers, body=body)

    def _resolve_base_url(self, resolved: ResolvedOperation) -> str:
        """Obtém a URL do ambiente e exige correspondência exata com a allowlist."""

        variable_name = resolved.profile.base_url_env
        if not variable_name:
            raise ExecutionValidationError(
                "BASE_URL_ENV_NOT_CONFIGURED",
                "o profile não declara a variável que contém a URL-base",
            )
        raw_base_url = self._environment.get(variable_name)
        if not raw_base_url:
            raise ExecutionValidationError(
                "BASE_URL_ENV_MISSING",
                f"a variável de ambiente '{variable_name}' não foi configurada",
            )

        base_url = _normalize_base_url(raw_base_url)
        try:
            allowed = {_normalize_base_url(value) for value in resolved.profile.allowed_base_urls}
        except ExecutionValidationError as exc:
            raise ExecutionValidationError(
                "INVALID_ALLOWLIST",
                "o profile contém uma URL inválida na allowlist",
            ) from exc
        if base_url not in allowed:
            raise ExecutionValidationError(
                "BASE_URL_NOT_ALLOWED",
                "a URL-base configurada não pertence à allowlist do conector",
            )
        return base_url

    async def _send_request(
        self,
        *,
        method: str,
        url: str,
        query: tuple[tuple[str, str], ...],
        headers: Mapping[str, str],
        body: Any,
        timeout_seconds: float,
    ) -> httpx.Response:
        """Envia partes já validadas usando o cliente injetado ou um cliente temporário."""

        request_arguments: dict[str, Any] = {
            "params": query,
            "headers": headers,
            "timeout": timeout_seconds,
        }
        if body is not NO_BODY:
            request_arguments["json"] = body
        if self._client is not None:
            return await self._client.request(method, url, **request_arguments)
        async with httpx.AsyncClient() as client:
            return await client.request(method, url, **request_arguments)

    @staticmethod
    def _elapsed_ms(started_at: float) -> float:
        """Calcula latência monotônica com precisão suficiente para traces e testes."""

        return max(0.0, round((perf_counter() - started_at) * 1000, 3))

    def _blocked(
        self,
        request: OperationExecutionRequest,
        started_at: float,
        code: str,
        message: str,
    ) -> OperationExecutionResult:
        """Cria um envelope de bloqueio, sempre sem status HTTP externo."""

        return OperationExecutionResult(
            connector_id=request.connector_id,
            operation_id=request.operation_id,
            outcome=ExecutionOutcome.BLOCKED,
            error=ExecutionErrorDetails(code=code, message=message),
            latency_ms=self._elapsed_ms(started_at),
        )

    def _failed(
        self,
        request: OperationExecutionRequest,
        started_at: float,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> OperationExecutionResult:
        """Cria um envelope para falhas ocorridas ao contatar ou interpretar o upstream."""

        return OperationExecutionResult(
            connector_id=request.connector_id,
            operation_id=request.operation_id,
            outcome=ExecutionOutcome.FAILED,
            status_code=status_code,
            error=ExecutionErrorDetails(code=code, message=message, retryable=retryable),
            latency_ms=self._elapsed_ms(started_at),
        )
