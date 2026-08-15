"""Descoberta e validação de conectores declarativos OpenAPI + YAML."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from copy import deepcopy
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

HTTP_METHODS: Final = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
READ_METHODS: Final = {"get", "head", "options"}


class ConnectorValidationError(ValueError):
    """Erro seguro e contextualizado na configuração de um conector."""


class UniqueKeyLoader(yaml.SafeLoader):
    """Loader que rejeita chaves YAML duplicadas em vez de descartar dados silenciosamente."""


def _construct_unique_mapping(
    loader: UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
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


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as source:
            content = yaml.load(source, Loader=UniqueKeyLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise ConnectorValidationError(f"não foi possível ler {path}: {exc}") from exc

    if not isinstance(content, dict):
        raise ConnectorValidationError(f"{path} deve conter um objeto YAML na raiz")
    return content


def _walk(value: Any) -> Iterator[tuple[str, Any]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key), child
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _validate_runtime_constraints(spec: dict[str, Any], path: Path) -> None:
    version = str(spec.get("openapi", ""))
    if not version.startswith("3."):
        raise ConnectorValidationError(f"{path}: somente OpenAPI 3.x é suportado")

    for key, value in _walk(spec):
        if key == "$ref" and (not isinstance(value, str) or not value.startswith("#/")):
            raise ConnectorValidationError(f"{path}: $ref externo não é permitido: {value!r}")
        if key == "format" and value in {"binary", "byte"}:
            raise ConnectorValidationError(f"{path}: payload binário não é suportado")

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

    try:
        validate_openapi(spec)
    except Exception as exc:
        raise ConnectorValidationError(f"contrato OpenAPI inválido em {path}: {exc}") from exc


def _operation_access(method: str) -> AccessMode:
    return AccessMode.READ if method in READ_METHODS else AccessMode.WRITE


def _operation_risk(access: AccessMode) -> RiskLevel:
    return RiskLevel.LOW if access == AccessMode.READ else RiskLevel.HIGH


def _build_operation(
    path: str,
    method: str,
    raw_operation: Mapping[str, Any],
    policy: OperationPolicy,
) -> OperationSummary:
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


def _parse_operations(
    spec: dict[str, Any],
    policies: dict[str, OperationPolicy],
) -> list[OperationSummary]:
    operations: list[OperationSummary] = []
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

            # Operações ausentes do perfil, inclusive leituras, nascem desabilitadas.
            policy = policies.get(operation_id, OperationPolicy())
            operations.append(_build_operation(path, normalized_method, raw_operation, policy))

    unknown_policies = set(policies) - seen_ids
    if unknown_policies:
        unknown = ", ".join(sorted(unknown_policies))
        raise ConnectorValidationError(
            f"políticas apontam para operationIds inexistentes: {unknown}"
        )

    return sorted(operations, key=lambda operation: (operation.path, operation.method))


class ConnectorCatalog:
    """Catálogo imutável em memória construído a partir do diretório configurado."""

    def __init__(self, connectors_dir: Path) -> None:
        self.connectors_dir = connectors_dir.resolve()
        self._connectors: dict[str, ConnectorDetails] = {}

    def load(self) -> None:
        if not self.connectors_dir.is_dir():
            raise ConnectorValidationError(
                f"diretório de conectores não encontrado: {self.connectors_dir}"
            )

        loaded: dict[str, ConnectorDetails] = {}
        for profile_path in sorted(self.connectors_dir.glob("*/profile.yaml")):
            connector = self._load_connector(profile_path)
            if connector.id in loaded:
                raise ConnectorValidationError(f"id de conector duplicado: '{connector.id}'")
            loaded[connector.id] = connector

        if not loaded:
            raise ConnectorValidationError("nenhum conector foi encontrado")
        self._connectors = loaded

    def _load_connector(self, profile_path: Path) -> ConnectorDetails:
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
        if not openapi_path.is_relative_to(connector_dir):
            raise ConnectorValidationError("o arquivo OpenAPI deve permanecer dentro do conector")
        spec = _load_yaml(openapi_path)
        _validate_runtime_constraints(spec, openapi_path)
        operations = _parse_operations(spec, profile.operations)

        domain_path = connector_dir / "domain.yaml"
        domain = _load_yaml(domain_path) if domain_path.exists() else {}
        context_fields = domain.get("context_fields", [])
        if not isinstance(context_fields, list) or not all(
            isinstance(field, str) for field in context_fields
        ):
            raise ConnectorValidationError(f"{domain_path}: context_fields deve ser uma lista")

        return ConnectorDetails(
            id=profile.id,
            name=profile.name,
            description=profile.description,
            openapi_version=str(spec["openapi"]),
            auth_type=profile.auth.type,
            operation_count=len(operations),
            enabled_operation_count=sum(operation.enabled for operation in operations),
            context_fields=context_fields,
            operations=operations,
        )

    def list(self) -> list[ConnectorSummary]:
        return [
            ConnectorSummary.model_validate(connector.model_dump(exclude={"operations"}))
            for connector in self._connectors.values()
        ]

    def get(self, connector_id: str) -> ConnectorDetails | None:
        connector = self._connectors.get(connector_id)
        return deepcopy(connector) if connector else None
