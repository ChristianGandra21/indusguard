"""Modelos Pydantic que tornam explícitos os contratos internos e externos.

Há dois grupos neste arquivo:

* modelos de configuração, usados para rejeitar profiles ambíguos ou com erros de digitação;
* modelos de resposta, usados pelo FastAPI para documentar e validar o que a aplicação expõe.

Usar os mesmos tipos no loader e na API evita dicionários sem contrato circulando pelo núcleo.
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AccessMode(StrEnum):
    """Distingue consultas de operações que podem alterar estado externo."""

    READ = "read"
    WRITE = "write"


class RiskLevel(StrEnum):
    """Graduação usada pela futura policy engine para decidir confirmação e bloqueio."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AuthProfile(BaseModel):
    """Descreve como o executor obterá autenticação sem armazenar seu valor no YAML.

    ``env`` contém apenas o nome da variável de ambiente. ``context_field`` aponta para um valor
    do contexto validado da execução, como ``user_id`` no conector Tractian.
    """

    # ``extra='forbid'`` transforma campos desconhecidos em erro. Isso detecta cedo um typo como
    # ``contex_field``, que de outra forma poderia desativar autenticação silenciosamente.
    model_config = ConfigDict(extra="forbid")

    type: Literal["none", "api_key_header", "api_key_query", "bearer", "context_header"]
    name: str | None = None
    env: str | None = None
    context_field: str | None = None

    @model_validator(mode="after")
    def validate_auth_fields(self) -> "AuthProfile":
        """Exige os campos específicos do método de autenticação selecionado."""

        if self.type in {"api_key_header", "api_key_query"} and not (self.name and self.env):
            raise ValueError("autenticação por API key exige 'name' e 'env'")
        if self.type == "bearer" and not self.env:
            raise ValueError("autenticação Bearer exige 'env'")
        if self.type == "context_header" and not (self.name and self.context_field):
            raise ValueError("context_header exige 'name' e 'context_field'")
        return self


class OperationPolicy(BaseModel):
    """Controles determinísticos aplicados a um único ``operationId``.

    Esses valores não são sugestões ao LLM. Eles serão avaliados por código antes da execução de
    uma tool, o que permite comparar futuramente o agente protegido com a variante prompt-only.
    """

    model_config = ConfigDict(extra="forbid")

    # False é proposital: uma operação nova no OpenAPI não ganha acesso automaticamente.
    enabled: bool = False
    access: AccessMode | None = None
    risk: RiskLevel | None = None
    permission: str | None = None

    # Uma ação direta é aquela explicitamente solicitada pela pessoa, não inferida pelo agente.
    requires_direct_request: bool = False
    requires_confirmation: bool = False
    justification_min_length: Annotated[int, Field(ge=0, le=1000)] = 0

    # Os limites impedem profiles acidentais com espera ou retries ilimitados.
    timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 10
    max_retries: Annotated[int, Field(ge=0, le=2)] = 0
    idempotent: bool = False

    # Campos listados aqui serão removidos de traces e persistência pelo futuro executor.
    redact_fields: list[str] = Field(default_factory=list)


class ConnectorProfile(BaseModel):
    """Manifesto técnico e de segurança de uma integração."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    name: str
    description: str
    openapi: str
    base_url_env: str | None = None
    # O ambiente escolhe a URL efetiva, mas ela ainda deverá pertencer a esta allowlist.
    allowed_base_urls: list[str] = Field(default_factory=list)
    auth: AuthProfile
    operations: dict[str, OperationPolicy]


class OperationSummary(BaseModel):
    """Visão consolidada de uma operação, segura para API, UI e geração futura de tools."""

    operation_id: str
    method: str
    path: str
    summary: str | None = None
    tags: list[str] = Field(default_factory=list)
    enabled: bool
    access: AccessMode
    risk: RiskLevel
    permission: str | None = None
    requires_direct_request: bool
    requires_confirmation: bool
    justification_min_length: int
    timeout_seconds: float
    max_retries: int
    idempotent: bool


class ConnectorSummary(BaseModel):
    """Resumo de conector usado em listagens, sem detalhes sensíveis de conexão."""

    id: str
    name: str
    description: str
    openapi_version: str
    auth_type: str
    operation_count: int
    enabled_operation_count: int
    context_fields: list[str] = Field(default_factory=list)


class ConnectorDetails(ConnectorSummary):
    """Resumo acrescido das operações consolidadas do conector."""

    operations: list[OperationSummary]


class HealthResponse(BaseModel):
    """Contrato mínimo do endpoint de liveness."""

    status: Literal["healthy"] = "healthy"


class ReadyResponse(BaseModel):
    """Contrato do endpoint de readiness após validação dos conectores."""

    status: Literal["ready"] = "ready"
    connector_count: int


class VersionResponse(BaseModel):
    """Metadados suficientes para correlacionar comportamento com um release."""

    version: str
    environment: str
    execution_mode: Literal["simulate", "execute"]
