"""Executor HTTP genérico construído sobre o catálogo validado de conectores.

Este primeiro corte vertical executa somente operações GET com parâmetros de path e autenticação
``none``. A limitação é explícita e segura: operações de escrita, query, body e autenticação são
bloqueadas até ganharem implementação e testes próprios.

Mesmo pequeno, o módulo já estabelece as fronteiras que permanecerão nas próximas etapas:

* recebe ``connector_id`` e ``operation_id``, nunca uma URL arbitrária;
* usa apenas operações habilitadas pelo profile;
* valida argumentos contra o schema OpenAPI antes da rede;
* resolve a URL-base pelo ambiente e confere a allowlist do profile;
* respeita o timeout da operação;
* devolve um envelope comum para sucesso, bloqueio ou falha.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from time import perf_counter
from typing import Any, Final
from urllib.parse import quote

import httpx
from jsonschema import SchemaError
from jsonschema.validators import validator_for

from indusguard_api.connectors import ConnectorCatalog, ResolvedOperation
from indusguard_api.schemas import (
    ExecutionErrorDetails,
    ExecutionOutcome,
    OperationExecutionRequest,
    OperationExecutionResult,
)

PATH_PLACEHOLDER: Final = re.compile(r"\{([^{}]+)\}")


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

    # A barra final não muda o destino e é removida para que a comparação da allowlist seja
    # previsível. O httpx também normaliza caixa do host e portas padrão.
    return str(url).rstrip("/")


def _serialize_path_value(value: Any) -> str:
    """Converte um valor JSON primitivo para a representação usada em um path HTTP."""

    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _render_path(resolved: ResolvedOperation, arguments: Mapping[str, Any]) -> str:
    """Valida parâmetros de path pelo OpenAPI e preenche o template da operação."""

    template = resolved.operation.path
    placeholders = set(PATH_PLACEHOLDER.findall(template))
    definitions: dict[str, dict[str, Any]] = {}

    for parameter in resolved.parameters:
        if parameter.get("in") != "path":
            continue
        if "$ref" in parameter:
            raise ExecutionValidationError(
                "UNSUPPORTED_PARAMETER_REFERENCE",
                "parâmetros de path por $ref ainda não são suportados neste incremento",
            )
        definitions[str(parameter["name"])] = parameter

    if placeholders != set(definitions):
        raise ExecutionValidationError(
            "INVALID_OPERATION_CONTRACT",
            "os placeholders do path não correspondem aos parâmetros declarados no OpenAPI",
        )

    provided = set(arguments)
    missing = placeholders - provided
    unexpected = provided - placeholders
    if missing:
        names = ", ".join(sorted(missing))
        raise ExecutionValidationError(
            "MISSING_PATH_ARGUMENT",
            f"faltam argumentos obrigatórios de path: {names}",
        )
    if unexpected:
        names = ", ".join(sorted(unexpected))
        raise ExecutionValidationError(
            "UNEXPECTED_PATH_ARGUMENT",
            f"foram enviados argumentos de path desconhecidos: {names}",
        )

    rendered = template
    for name in sorted(placeholders):
        value = arguments[name]
        schema = definitions[name].get("schema", {})
        try:
            validator_class = validator_for(schema)
            validator_class.check_schema(schema)
            errors = sorted(
                validator_class(schema).iter_errors(value),
                key=lambda error: error.path,
            )
        except SchemaError as exc:
            raise ExecutionValidationError(
                "INVALID_OPERATION_CONTRACT",
                f"o schema do argumento de path '{name}' é inválido",
            ) from exc
        if errors:
            raise ExecutionValidationError(
                "INVALID_PATH_ARGUMENT",
                f"argumento de path '{name}' inválido: {errors[0].message}",
            )

        # ``safe=''`` codifica inclusive barras. Assim, um valor não consegue criar segmentos de
        # path adicionais ou escapar do endpoint descrito pelo OpenAPI.
        encoded = quote(_serialize_path_value(value), safe="")
        rendered = rendered.replace(f"{{{name}}}", encoded)

    return rendered


class HttpExecutor:
    """Executa operações conhecidas usando dependências injetáveis e defaults seguros.

    O cliente HTTP pode ser injetado nos testes com ``httpx.MockTransport``. Em produção, quando
    nenhum cliente é fornecido, o executor cria e fecha um cliente para a chamada. Um pool
    compartilhado será conectado ao lifespan do FastAPI quando houver endpoint de execução.
    """

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
        """Valida, executa e normaliza uma única operação GET."""

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
        if resolved.operation.method != "GET":
            return self._blocked(
                request,
                started_at,
                "METHOD_NOT_SUPPORTED",
                "este incremento executa somente operações GET; escritas continuam bloqueadas",
            )
        if resolved.profile.auth.type != "none":
            return self._blocked(
                request,
                started_at,
                "AUTH_NOT_SUPPORTED",
                "este incremento executa somente conectores sem autenticação",
            )

        try:
            rendered_path = _render_path(resolved, request.arguments.path)
            base_url = self._resolve_base_url(resolved)
        except ExecutionValidationError as exc:
            return self._blocked(request, started_at, exc.code, str(exc))

        url = f"{base_url}{rendered_path}"
        try:
            response = await self._send_get(url, resolved.operation.timeout_seconds)
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
            # A URL recebida não é incluída na mensagem: ela pode conter informação que não deve
            # aparecer em trace ou resposta ao agente.
            raise ExecutionValidationError(
                "BASE_URL_NOT_ALLOWED",
                "a URL-base configurada não pertence à allowlist do conector",
            )
        return base_url

    async def _send_get(self, url: str, timeout_seconds: float) -> httpx.Response:
        """Executa o GET usando o cliente injetado ou um cliente de curta duração."""

        if self._client is not None:
            return await self._client.get(url, timeout=timeout_seconds)
        async with httpx.AsyncClient() as client:
            return await client.get(url, timeout=timeout_seconds)

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
