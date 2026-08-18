"""Descoberta e validação de conectores declarativos OpenAPI + YAML.

Este módulo é a fronteira entre arquivos fornecidos por cada integração e o núcleo do
IndusGuard. Ele transforma três arquivos declarativos em objetos Python confiáveis:

* ``openapi.yaml`` descreve os endpoints e seus formatos;
* ``profile.yaml`` decide quais endpoints podem virar tools e sob quais políticas;
* ``domain.yaml`` adiciona vocabulário e campos de contexto do domínio.

Nesta etapa o catálogo apenas lê e valida metadados. Ele ainda não executa requisições HTTP e
não acessa credenciais. Essa separação é intencional: primeiro provamos que uma operação é
conhecida e permitida; somente depois o executor, em outro módulo, poderá chamá-la.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml
from openapi_spec_validator import validate as validate_openapi
from pydantic import ValidationError

from indusguard_api.schemas import (
    AccessMode,
    ConnectorDetails,
    ConnectorProfile,
    ConnectorSummary,
    OperationPolicy,
    OperationSummary,
    RiskLevel,
)

# O OpenAPI permite outros campos dentro de um path, como ``parameters``. Esta allowlist evita
# interpretar esses campos acidentalmente como operações executáveis.
HTTP_METHODS: Final = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}

# O método HTTP fornece um primeiro limite determinístico entre consulta e mutação. A política
# declarada no profile não pode contradizer esse limite.
READ_METHODS: Final = {"get", "head", "options"}


@dataclass(frozen=True)
class RuntimeOperation:
    """Operação consolidada acrescida dos metadados necessários para executá-la.

    ``OperationSummary`` continua sendo a visão pública. Os parâmetros OpenAPI permanecem
    internos porque o executor precisa deles para validar argumentos, mas as rotas de catálogo
    não precisam expor toda a especificação original.
    """

    summary: OperationSummary
    parameters: tuple[dict[str, Any], ...]
    request_body: dict[str, Any] | None
    reference_document: dict[str, Any]


@dataclass(frozen=True)
class LoadedConnector:
    """Representação interna completa de um conector validado."""

    profile: ConnectorProfile
    details: ConnectorDetails
    operations: dict[str, RuntimeOperation]


@dataclass(frozen=True)
class ResolvedOperation:
    """Cópia segura entregue pelo catálogo a um consumidor interno como o executor."""

    profile: ConnectorProfile
    operation: OperationSummary
    parameters: tuple[dict[str, Any], ...]
    request_body: dict[str, Any] | None
    reference_document: dict[str, Any]


class ConnectorValidationError(ValueError):
    """Indica que um conector não é seguro ou consistente o bastante para ser carregado."""


class UniqueKeyLoader(yaml.SafeLoader):
    """Loader que rejeita chaves YAML duplicadas em vez de descartar dados silenciosamente.

    A maioria dos parsers YAML mantém apenas a última ocorrência de uma chave. Em OpenAPI isso
    pode fazer um path GET desaparecer quando o mesmo path é repetido para PATCH. Falhar no
    startup torna o problema visível antes que uma tool seja gerada incorretamente.
    """


def _construct_unique_mapping(
    loader: UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    """Constrói um mapa YAML garantindo que cada chave apareça uma única vez."""

    # ``flatten_mapping`` resolve aliases e merges YAML antes da verificação. Assim, a regra de
    # unicidade também vale para profiles que reutilizam configurações com âncoras.
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConnectorValidationError(
                f"chave YAML duplicada '{key}' na linha {key_node.start_mark.line + 1}"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


# Substitui somente o construtor de mapas; strings, listas e números continuam usando o
# comportamento seguro do ``yaml.SafeLoader``.
UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_yaml(path: Path) -> dict[str, Any]:
    """Lê YAML como objeto e converte erros de parser em erros do domínio de conectores."""

    try:
        with path.open(encoding="utf-8") as source:
            content = yaml.load(source, Loader=UniqueKeyLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise ConnectorValidationError(f"não foi possível ler {path}: {exc}") from exc

    if not isinstance(content, dict):
        raise ConnectorValidationError(f"{path} deve conter um objeto YAML na raiz")
    return content


def _walk(value: Any) -> Iterator[tuple[str, Any]]:
    """Percorre recursivamente mapas e listas para aplicar regras em todo o contrato."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key), child
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _validate_runtime_constraints(spec: dict[str, Any], path: Path) -> None:
    """Valida o OpenAPI e os limites deliberados da primeira versão do runtime.

    O validador oficial verifica a estrutura do OpenAPI. As verificações anteriores representam
    escolhas do IndusGuard: somente referências locais, JSON e ausência de payload binário.
    Recusar cedo mantém o executor menor e reduz superfícies de SSRF e exfiltração.
    """

    version = str(spec.get("openapi", ""))
    if not version.startswith("3."):
        raise ConnectorValidationError(f"{path}: somente OpenAPI 3.x é suportado")

    # Referências remotas poderiam fazer o loader buscar arquivos ou hosts fora do conector.
    for key, value in _walk(spec):
        if key == "$ref" and (not isinstance(value, str) or not value.startswith("#/")):
            raise ConnectorValidationError(f"{path}: $ref externo não é permitido: {value!r}")
        if key == "format" and value in {"binary", "byte"}:
            raise ConnectorValidationError(f"{path}: payload binário não é suportado")

    # A v1 é orientada a REST JSON. Uploads, streams e outros formatos exigirão executores
    # especializados e, por isso, são rejeitados por enquanto.
    for endpoint, path_item in spec.get("paths", {}).items():
        if not isinstance(path_item, Mapping):
            continue
        for method, operation in path_item.items():
            if method.lower() not in HTTP_METHODS or not isinstance(operation, Mapping):
                continue
            request_content = operation.get("requestBody", {}).get("content", {})
            response_objects = operation.get("responses", {}).values()
            response_content_types = {
                content_type
                for response in response_objects
                if isinstance(response, Mapping)
                for content_type in response.get("content", {})
            }
            unsupported = {
                content_type
                for content_type in {*request_content, *response_content_types}
                if content_type != "application/json" and not content_type.endswith("+json")
            }
            if unsupported:
                raise ConnectorValidationError(
                    f"{path}: {method.upper()} {endpoint} usa conteúdo não JSON: "
                    f"{', '.join(sorted(unsupported))}"
                )

    # Só depois das restrições locais aplicamos a validação completa da especificação.
    try:
        validate_openapi(spec)
    except Exception as exc:
        raise ConnectorValidationError(f"contrato OpenAPI inválido em {path}: {exc}") from exc


def _operation_access(method: str) -> AccessMode:
    """Classifica o efeito esperado do método sem depender de interpretação do modelo."""

    return AccessMode.READ if method in READ_METHODS else AccessMode.WRITE


def _operation_risk(access: AccessMode) -> RiskLevel:
    """Fornece um default conservador quando o perfil não explicita o risco."""

    return RiskLevel.LOW if access == AccessMode.READ else RiskLevel.HIGH


def _build_operation(
    path: str,
    method: str,
    raw_operation: Mapping[str, Any],
    policy: OperationPolicy,
) -> OperationSummary:
    """Combina a descrição técnica do OpenAPI com a política local da operação."""

    access = _operation_access(method)
    if policy.access is not None and policy.access != access:
        raise ConnectorValidationError(
            f"operationId '{raw_operation['operationId']}' declara access={policy.access}, "
            f"mas o método {method.upper()} implica access={access}"
        )

    return OperationSummary(
        operation_id=raw_operation["operationId"],
        method=method.upper(),
        path=path,
        summary=raw_operation.get("summary") or raw_operation.get("description"),
        tags=list(raw_operation.get("tags", [])),
        enabled=policy.enabled,
        access=access,
        risk=policy.risk or _operation_risk(access),
        permission=policy.permission,
        requires_direct_request=policy.requires_direct_request,
        requires_confirmation=policy.requires_confirmation,
        justification_min_length=policy.justification_min_length,
        timeout_seconds=policy.timeout_seconds,
        max_retries=policy.max_retries,
        idempotent=policy.idempotent,
    )


def _resolve_json_pointer(document: Mapping[str, Any], reference: str) -> Any:
    """Resolve um JSON Pointer local sem acessar arquivos ou rede.

    ``_validate_runtime_constraints`` já impede referências externas. Esta função implementa a
    parte restante de ``$ref`` necessária em runtime e trata os escapes definidos por RFC 6901.
    """

    if not reference.startswith("#/"):
        raise ConnectorValidationError(f"$ref local inválido: {reference!r}")

    current: Any = document
    for raw_token in reference[2:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        try:
            if isinstance(current, Mapping):
                current = current[token]
            elif isinstance(current, list):
                current = current[int(token)]
            else:
                raise KeyError(token)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ConnectorValidationError(f"$ref local não encontrado: {reference}") from exc
    return deepcopy(current)


def _resolve_reference_object(
    document: Mapping[str, Any],
    value: Mapping[str, Any],
    *,
    seen: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Resolve uma cadeia de Reference Objects e detecta ciclos de configuração."""

    reference = value.get("$ref")
    if reference is None:
        return deepcopy(dict(value))
    if not isinstance(reference, str):
        raise ConnectorValidationError("$ref precisa ser uma string")
    if reference in seen:
        raise ConnectorValidationError(f"ciclo de $ref detectado em {reference}")

    target = _resolve_json_pointer(document, reference)
    if not isinstance(target, Mapping):
        raise ConnectorValidationError(f"$ref precisa apontar para um objeto: {reference}")
    resolved = _resolve_reference_object(document, target, seen=seen | {reference})

    # OpenAPI 3.1 permite summary/description ao lado de $ref. Preservar qualquer sibling é mais
    # previsível que descartá-lo silenciosamente e não amplia o destino da referência.
    resolved.update({key: deepcopy(child) for key, child in value.items() if key != "$ref"})
    return resolved


def _merge_parameters(
    spec: Mapping[str, Any],
    path_item: Mapping[str, Any],
    raw_operation: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    """Combina parâmetros do path e da operação conforme a regra de override do OpenAPI.

    Um parâmetro declarado diretamente na operação substitui outro com o mesmo par ``(in,
    name)`` declarado no path. Referências locais são resolvidas antes da comparação; assim, um
    parâmetro direto da operação também consegue substituir outro referenciado no path.
    """

    merged: dict[tuple[str, str], dict[str, Any]] = {}
    parameter_groups = (path_item.get("parameters", []), raw_operation.get("parameters", []))
    for parameters in parameter_groups:
        for parameter in parameters:
            resolved = _resolve_reference_object(spec, parameter)
            key = (str(resolved.get("in", "")), str(resolved.get("name", "")))
            merged[key] = resolved
    return tuple(merged.values())


def _request_body(
    spec: Mapping[str, Any],
    raw_operation: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Obtém o requestBody já resolvido quando a operação declara um."""

    value = raw_operation.get("requestBody")
    if value is None:
        return None
    return _resolve_reference_object(spec, value)


def _parse_operations(
    spec: dict[str, Any],
    policies: dict[str, OperationPolicy],
) -> list[RuntimeOperation]:
    """Extrai operações do OpenAPI e garante correspondência exata com o profile.

    Uma operação existente apenas no OpenAPI é conhecida, mas nasce desabilitada. Já uma política
    que aponta para um ``operationId`` inexistente é provavelmente um erro de digitação ou drift
    de contrato e invalida o conector inteiro.
    """

    operations: list[RuntimeOperation] = []
    seen_ids: set[str] = set()

    for path, path_item in spec.get("paths", {}).items():
        if not isinstance(path_item, Mapping):
            continue
        for method, raw_operation in path_item.items():
            normalized_method = method.lower()
            if normalized_method not in HTTP_METHODS or not isinstance(raw_operation, Mapping):
                continue
            operation_id = raw_operation.get("operationId")
            if not operation_id:
                raise ConnectorValidationError(
                    f"{normalized_method.upper()} {path} precisa declarar operationId"
                )
            if operation_id in seen_ids:
                raise ConnectorValidationError(f"operationId duplicado: '{operation_id}'")
            seen_ids.add(operation_id)

            # Segurança por default: descobrir um endpoint novo não o torna automaticamente
            # disponível ao agente, nem mesmo quando ele é somente leitura.
            policy = policies.get(operation_id, OperationPolicy())
            summary = _build_operation(path, normalized_method, raw_operation, policy)
            operations.append(
                RuntimeOperation(
                    summary=summary,
                    parameters=_merge_parameters(spec, path_item, raw_operation),
                    request_body=_request_body(spec, raw_operation),
                    # O documento é a raiz usada para resolver $refs que aparecem dentro de
                    # schemas. Ele nunca é exposto pelas rotas públicas.
                    reference_document=spec,
                )
            )

    unknown_policies = set(policies) - seen_ids
    if unknown_policies:
        unknown = ", ".join(sorted(unknown_policies))
        raise ConnectorValidationError(
            f"políticas apontam para operationIds inexistentes: {unknown}"
        )

    return sorted(
        operations,
        key=lambda operation: (operation.summary.path, operation.summary.method),
    )


class ConnectorCatalog:
    """Catálogo em memória construído a partir do diretório configurado.

    O catálogo é recarregado como um conjunto completo: primeiro todos os conectores são
    validados em uma variável temporária e só então substituem o estado atual. Isso evita expor
    um catálogo parcialmente carregado quando um dos profiles contém erro.
    """

    def __init__(self, connectors_dir: Path) -> None:
        self.connectors_dir = connectors_dir.resolve()
        self._connectors: dict[str, LoadedConnector] = {}

    def load(self) -> None:
        """Descobre ``*/profile.yaml`` e carrega todos os conectores de forma fail-fast."""

        if not self.connectors_dir.is_dir():
            raise ConnectorValidationError(
                f"diretório de conectores não encontrado: {self.connectors_dir}"
            )

        loaded: dict[str, LoadedConnector] = {}
        # Ordenar torna respostas e testes reprodutíveis entre sistemas de arquivos diferentes.
        for profile_path in sorted(self.connectors_dir.glob("*/profile.yaml")):
            connector = self._load_connector(profile_path)
            connector_id = connector.details.id
            if connector_id in loaded:
                raise ConnectorValidationError(f"id de conector duplicado: '{connector_id}'")
            loaded[connector_id] = connector

        if not loaded:
            raise ConnectorValidationError("nenhum conector foi encontrado")
        self._connectors = loaded

    def _load_connector(self, profile_path: Path) -> LoadedConnector:
        """Valida os três arquivos de um conector e produz sua visão pública consolidada."""

        connector_dir = profile_path.parent.resolve()
        try:
            profile = ConnectorProfile.model_validate(_load_yaml(profile_path))
        except ValidationError as exc:
            raise ConnectorValidationError(f"perfil inválido em {profile_path}: {exc}") from exc

        if profile.id != connector_dir.name:
            raise ConnectorValidationError(
                f"id '{profile.id}' deve corresponder ao diretório '{connector_dir.name}'"
            )

        openapi_path = (connector_dir / profile.openapi).resolve()
        # Impede ``../../arquivo`` no profile. Mesmo um arquivo local só é confiável quando faz
        # parte do diretório do conector que está sendo validado.
        if not openapi_path.is_relative_to(connector_dir):
            raise ConnectorValidationError("o arquivo OpenAPI deve permanecer dentro do conector")
        spec = _load_yaml(openapi_path)
        _validate_runtime_constraints(spec, openapi_path)
        operations = _parse_operations(spec, profile.operations)

        # ``domain.yaml`` é opcional no loader para permitir contratos puramente técnicos. Quando
        # existe, seus campos de contexto precisam ser simples e previsíveis para a futura UI.
        domain_path = connector_dir / "domain.yaml"
        domain = _load_yaml(domain_path) if domain_path.exists() else {}
        context_fields = domain.get("context_fields", [])
        if not isinstance(context_fields, list) or not all(
            isinstance(field, str) for field in context_fields
        ):
            raise ConnectorValidationError(f"{domain_path}: context_fields deve ser uma lista")
        if len(context_fields) != len(set(context_fields)):
            raise ConnectorValidationError(f"{domain_path}: context_fields contém duplicatas")
        if (
            profile.auth.type == "context_header"
            and profile.auth.context_field not in context_fields
        ):
            raise ConnectorValidationError(
                f"{domain_path}: autenticação exige o context_field '{profile.auth.context_field}'"
            )

        details = ConnectorDetails(
            id=profile.id,
            name=profile.name,
            description=profile.description,
            openapi_version=str(spec["openapi"]),
            auth_type=profile.auth.type,
            operation_count=len(operations),
            enabled_operation_count=sum(operation.summary.enabled for operation in operations),
            context_fields=context_fields,
            operations=[operation.summary for operation in operations],
        )
        return LoadedConnector(
            profile=profile,
            details=details,
            operations={operation.summary.operation_id: operation for operation in operations},
        )

    def list(self) -> list[ConnectorSummary]:
        """Retorna a visão resumida, adequada para a listagem da API e da futura interface."""

        return [
            ConnectorSummary.model_validate(connector.details.model_dump(exclude={"operations"}))
            for connector in self._connectors.values()
        ]

    def get(self, connector_id: str) -> ConnectorDetails | None:
        """Retorna uma cópia para impedir que consumidores alterem o catálogo compartilhado."""

        connector = self._connectors.get(connector_id)
        return deepcopy(connector.details) if connector else None

    def resolve_operation(
        self,
        connector_id: str,
        operation_id: str,
    ) -> ResolvedOperation | None:
        """Resolve metadados internos sem permitir mutação do catálogo compartilhado.

        Este método é deliberadamente separado de ``get()``. Assim, as rotas públicas continuam
        recebendo somente ``ConnectorDetails``, enquanto o executor obtém profile, allowlist e
        parâmetros OpenAPI por uma interface explícita.
        """

        connector = self._connectors.get(connector_id)
        if connector is None:
            return None
        operation = connector.operations.get(operation_id)
        if operation is None:
            return None
        return ResolvedOperation(
            profile=deepcopy(connector.profile),
            operation=deepcopy(operation.summary),
            parameters=deepcopy(operation.parameters),
            request_body=deepcopy(operation.request_body),
            reference_document=deepcopy(operation.reference_document),
        )
