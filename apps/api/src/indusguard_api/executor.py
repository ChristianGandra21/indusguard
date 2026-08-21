"""Executor HTTP genérico construído sobre o catálogo validado de conectores.

O terceiro corte vertical executa operações GET com parâmetros validados, cinco modos de
autenticação, retry idempotente e redaction. Operações mutáveis são simuladas por default; uma
chamada direta ao executor nunca realiza escrita real, mesmo com a policy engine implementada.

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
from asyncio import sleep as async_sleep
from collections.abc import Awaitable, Callable, Mapping
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
    AccessMode,
    ExecutionArguments,
    ExecutionErrorDetails,
    ExecutionMode,
    ExecutionOutcome,
    OperationExecutionRequest,
    OperationExecutionResult,
    SimulatedAction,
)

PATH_PLACEHOLDER: Final = re.compile(r"\{([^{}]+)\}")
HEADER_NAME: Final = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
NO_BODY: Final = object()
REDACTED_VALUE: Final = "[REDACTED]"


@dataclass(frozen=True)
class PreparedRequest:
    """Partes HTTP validadas, ainda sem abrir conexão com o upstream."""

    path: str
    query: tuple[tuple[str, str], ...]
    headers: dict[str, str]
    body: Any
    sensitive_values: frozenset[str]


@dataclass(frozen=True)
class PreparedAuth:
    """Credenciais já validadas e separadas das partes fornecidas pelo agente."""

    headers: dict[str, str]
    query: tuple[tuple[str, str], ...]
    sensitive_values: frozenset[str] = frozenset()


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


def _read_auth_secret(
    environment: Mapping[str, str],
    variable_name: str,
) -> str:
    """Lê uma credencial sem copiá-la para mensagens, modelos ou envelopes."""

    value = environment.get(variable_name)
    if not value:
        raise ExecutionValidationError(
            "AUTH_ENV_MISSING",
            f"a variável de autenticação '{variable_name}' não foi configurada",
        )
    return value


def _validate_auth_header(name: str, value: str) -> None:
    """Impede nomes inválidos e injeção de headers em credenciais externas."""

    if not HEADER_NAME.fullmatch(name):
        raise ExecutionValidationError(
            "INVALID_OPERATION_CONTRACT",
            "o profile contém um nome inválido para o header de autenticação",
        )
    if "\r" in value or "\n" in value:
        raise ExecutionValidationError(
            "INVALID_AUTH_SECRET",
            "a credencial configurada contém um valor inválido",
        )


def _ensure_auth_header_is_reserved(
    header_name: str,
    provided_headers: Mapping[str, Any],
) -> None:
    """Garante que um argumento nunca possa substituir autenticação controlada pelo runtime."""

    if header_name.lower() in {name.lower() for name in provided_headers}:
        raise ExecutionValidationError(
            "RESERVED_AUTH_HEADER",
            f"o header de autenticação '{header_name}' não pode vir dos argumentos",
        )


def _build_auth_material(
    resolved: ResolvedOperation,
    context: Mapping[str, Any],
    provided_headers: Mapping[str, Any],
    provided_query: Mapping[str, Any],
    environment: Mapping[str, str],
    *,
    include_secrets: bool,
) -> PreparedAuth:
    """Monta autenticação do contexto/ambiente sem aceitar valores vindos do agente.

    Simulações usam ``include_secrets=False``: validam campos reservados e o contexto, porém nem
    sequer leem API keys ou tokens do ambiente.
    """

    auth = resolved.profile.auth
    if auth.type == "none":
        return PreparedAuth(headers={}, query=())

    if auth.type == "context_header":
        header_name = auth.name or ""
        context_field = auth.context_field or ""
        _validate_auth_header(header_name, "")
        _ensure_auth_header_is_reserved(header_name, provided_headers)
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
        return PreparedAuth(headers={header_name: serialized}, query=())

    if auth.type == "api_key_query":
        query_name = auth.name or ""
        if query_name in provided_query:
            raise ExecutionValidationError(
                "RESERVED_AUTH_QUERY",
                f"o parâmetro de autenticação '{query_name}' não pode vir dos argumentos",
            )
        if not include_secrets:
            return PreparedAuth(headers={}, query=())
        secret = _read_auth_secret(environment, auth.env or "")
        return PreparedAuth(
            headers={},
            query=((query_name, secret),),
            sensitive_values=frozenset({secret}),
        )

    if auth.type in {"api_key_header", "bearer"}:
        header_name = auth.name or "" if auth.type == "api_key_header" else "Authorization"
        _validate_auth_header(header_name, "")
        _ensure_auth_header_is_reserved(header_name, provided_headers)
        if not include_secrets:
            return PreparedAuth(headers={}, query=())

        secret = _read_auth_secret(environment, auth.env or "")
        value = secret if auth.type == "api_key_header" else f"Bearer {secret}"
        _validate_auth_header(header_name, value)
        return PreparedAuth(
            headers={header_name: value},
            query=(),
            sensitive_values=frozenset({secret, value}),
        )

    raise ExecutionValidationError(
        "AUTH_NOT_SUPPORTED",
        f"autenticação '{auth.type}' não é suportada",
    )


def _redact(
    value: Any,
    fields: frozenset[str],
    sensitive_values: frozenset[str] = frozenset(),
) -> Any:
    """Substitui recursivamente valores de chaves sensíveis sem alterar o objeto original."""

    if isinstance(value, Mapping):
        return {
            key: (
                REDACTED_VALUE if str(key) in fields else _redact(child, fields, sensitive_values)
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact(child, fields, sensitive_values) for child in value]
    if isinstance(value, tuple):
        return tuple(_redact(child, fields, sensitive_values) for child in value)
    if isinstance(value, str):
        redacted = value
        for sensitive in sorted(sensitive_values, key=len, reverse=True):
            if sensitive:
                redacted = redacted.replace(sensitive, REDACTED_VALUE)
        return redacted
    return value


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
        execution_mode: ExecutionMode = "simulate",
        retry_base_delay_seconds: float = 0.1,
        sleeper: Callable[[float], Awaitable[None]] = async_sleep,
    ) -> None:
        if execution_mode not in {"simulate", "execute"}:
            raise ValueError("execution_mode precisa ser 'simulate' ou 'execute'")
        if retry_base_delay_seconds < 0:
            raise ValueError("retry_base_delay_seconds não pode ser negativo")
        self._catalog = catalog
        self._environment = os.environ if environment is None else environment
        self._client = client
        self._execution_mode = execution_mode
        self._retry_base_delay_seconds = retry_base_delay_seconds
        self._sleeper = sleeper

    @property
    def execution_mode(self) -> ExecutionMode:
        """Expõe somente leitura do modo para impedir divergência com a policy engine."""

        return self._execution_mode

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

        if resolved.operation.access is AccessMode.WRITE:
            try:
                # Uma simulação valida identidade e campos reservados, mas não lê credenciais do
                # ambiente e não exige que a API externa esteja configurada.
                prepared = self._prepare_request(
                    resolved,
                    request,
                    include_auth_secrets=False,
                )
            except ExecutionValidationError as exc:
                return self._blocked(request, started_at, exc.code, str(exc))

            if self._execution_mode == "simulate":
                return self._simulated(request, resolved, prepared, started_at)
            return self._blocked(
                request,
                started_at,
                "WRITE_POLICY_REQUIRED",
                "escrita real não pode ser chamada diretamente no HttpExecutor",
            )

        try:
            prepared = self._prepare_request(
                resolved,
                request,
                include_auth_secrets=True,
            )
            base_url = self._resolve_base_url(resolved)
        except ExecutionValidationError as exc:
            return self._blocked(request, started_at, exc.code, str(exc))

        # A primeira versão de leitura executa somente GET. HEAD/OPTIONS conhecidos continuam
        # explícitos no catálogo, mas exigirão tratamento de resposta próprio antes da rede.
        if resolved.operation.method != "GET":
            return self._blocked(
                request,
                started_at,
                "METHOD_NOT_SUPPORTED",
                "o executor de leitura suporta somente GET neste incremento",
            )

        url = f"{base_url}{prepared.path}"
        max_attempts = 1 + (resolved.operation.max_retries if resolved.operation.idempotent else 0)
        response: httpx.Response | None = None
        attempts = 0
        for attempts in range(1, max_attempts + 1):
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
                if attempts < max_attempts:
                    await self._wait_before_retry(attempts)
                    continue
                return self._failed(
                    request,
                    started_at,
                    "UPSTREAM_TIMEOUT",
                    "a API conectada excedeu o timeout configurado",
                    retryable=True,
                    attempts=attempts,
                )
            except httpx.RequestError:
                if attempts < max_attempts:
                    await self._wait_before_retry(attempts)
                    continue
                return self._failed(
                    request,
                    started_at,
                    "UPSTREAM_CONNECTION_ERROR",
                    "não foi possível conectar à API configurada",
                    retryable=True,
                    attempts=attempts,
                )

            if self._is_retryable_status(response.status_code) and attempts < max_attempts:
                await self._wait_before_retry(attempts)
                continue
            break

        # O loop sempre realiza ao menos uma tentativa; a asserção documenta esse invariante para
        # o type checker sem criar um fallback que esconderia um erro de programação.
        assert response is not None

        try:
            data = response.json() if response.content else None
        except ValueError:
            return self._failed(
                request,
                started_at,
                "INVALID_JSON_RESPONSE",
                "a API conectada retornou um corpo que não é JSON válido",
                status_code=response.status_code,
                attempts=attempts,
            )

        data = _redact(
            data,
            self._redaction_fields(resolved),
            prepared.sensitive_values,
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
                attempts=attempts,
                latency_ms=self._elapsed_ms(started_at),
            )

        return OperationExecutionResult(
            connector_id=request.connector_id,
            operation_id=request.operation_id,
            outcome=ExecutionOutcome.EXECUTED,
            status_code=response.status_code,
            data=data,
            attempts=attempts,
            latency_ms=self._elapsed_ms(started_at),
        )

    def _prepare_request(
        self,
        resolved: ResolvedOperation,
        request: OperationExecutionRequest,
        *,
        include_auth_secrets: bool,
    ) -> PreparedRequest:
        """Compila os argumentos somente depois de todas as validações locais."""

        path = _render_path(resolved, request.arguments.path)
        auth = _build_auth_material(
            resolved,
            request.context,
            request.arguments.headers,
            request.arguments.query,
            self._environment,
            include_secrets=include_auth_secrets,
        )
        query = _build_query(resolved, request.arguments.query) + auth.query
        headers = _build_headers(resolved, request.arguments.headers)
        headers.update(auth.headers)
        body = _build_body(resolved, request.arguments)
        return PreparedRequest(
            path=path,
            query=query,
            headers=headers,
            body=body,
            sensitive_values=auth.sensitive_values,
        )

    @staticmethod
    def _redaction_fields(resolved: ResolvedOperation) -> frozenset[str]:
        """Obtém a política interna sem expô-la no catálogo público."""

        policy = resolved.profile.operations.get(resolved.operation.operation_id)
        return frozenset(policy.redact_fields if policy is not None else ())

    def _simulated(
        self,
        request: OperationExecutionRequest,
        resolved: ResolvedOperation,
        prepared: PreparedRequest,
        started_at: float,
    ) -> OperationExecutionResult:
        """Produz uma prévia redigida sem resolver URL externa ou abrir conexão."""

        auth = resolved.profile.auth
        header_names = set(prepared.headers)
        if auth.type in {"api_key_header", "context_header"} and auth.name:
            header_names.add(auth.name)
        elif auth.type == "bearer":
            header_names.add("Authorization")

        redaction_fields = self._redaction_fields(resolved)
        body_present = prepared.body is not NO_BODY
        simulation = SimulatedAction(
            method=resolved.operation.method,
            path=prepared.path,
            # A query de autenticação nunca faz parte da entrada aceita do agente e é omitida.
            query=_redact(request.arguments.query, redaction_fields),
            header_names=sorted(header_names, key=str.lower),
            body_present=body_present,
            body=_redact(prepared.body, redaction_fields) if body_present else None,
            auth_type=auth.type,
        )
        return OperationExecutionResult(
            connector_id=request.connector_id,
            operation_id=request.operation_id,
            outcome=ExecutionOutcome.SIMULATED,
            simulation=simulation,
            latency_ms=self._elapsed_ms(started_at),
        )

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        """Repete apenas throttling e falhas do servidor, nunca erros 4xx comuns."""

        return status_code == 429 or status_code >= 500

    async def _wait_before_retry(self, failed_attempt: int) -> None:
        """Aplica backoff exponencial pequeno e limitado entre tentativas."""

        delay = min(self._retry_base_delay_seconds * (2 ** (failed_attempt - 1)), 2.0)
        if delay > 0:
            await self._sleeper(delay)

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
        attempts: int = 0,
    ) -> OperationExecutionResult:
        """Cria um envelope para falhas ocorridas ao contatar ou interpretar o upstream."""

        return OperationExecutionResult(
            connector_id=request.connector_id,
            operation_id=request.operation_id,
            outcome=ExecutionOutcome.FAILED,
            status_code=status_code,
            error=ExecutionErrorDetails(code=code, message=message, retryable=retryable),
            attempts=attempts,
            latency_ms=self._elapsed_ms(started_at),
        )
