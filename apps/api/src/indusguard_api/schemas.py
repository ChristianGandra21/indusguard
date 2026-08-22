"""Modelos Pydantic que tornam explícitos os contratos internos e externos.

Há dois grupos neste arquivo:

* modelos de configuração, usados para rejeitar profiles ambíguos ou com erros de digitação;
* modelos de resposta, usados pelo FastAPI para documentar e validar o que a aplicação expõe.

Usar os mesmos tipos no loader e na API evita dicionários sem contrato circulando pelo núcleo.
"""

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

AuthType = Literal["none", "api_key_header", "api_key_query", "bearer", "context_header"]
ExecutionMode = Literal["simulate", "execute"]
ScopeValue = str | int | float | bool


class AccessMode(StrEnum):
    """Distingue consultas de operações que podem alterar estado externo."""

    READ = "read"
    WRITE = "write"


class RiskLevel(StrEnum):
    """Graduação exposta pela policy engine para decisão, explicação e release gates."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ExecutionOutcome(StrEnum):
    """Resultado de alto nível produzido pelo executor HTTP.

    Separar ``blocked`` de ``failed`` é importante para observabilidade: um bloqueio significa
    que uma regra de segurança funcionou antes da rede; uma falha significa que uma chamada
    permitida não conseguiu produzir uma resposta utilizável.
    """

    EXECUTED = "executed"
    SIMULATED = "simulated"
    BLOCKED = "blocked"
    FAILED = "failed"


class PolicyOutcome(StrEnum):
    """Decisões que a policy engine pode tomar antes do executor HTTP."""

    ALLOW = "allow"
    SIMULATE = "simulate"
    REQUIRE_CONFIRMATION = "require_confirmation"
    BLOCK = "block"


class PolicyReasonCode(StrEnum):
    """Códigos estáveis para testes, métricas e explicações na futura interface."""

    CONNECTOR_NOT_FOUND = "CONNECTOR_NOT_FOUND"
    OPERATION_NOT_FOUND = "OPERATION_NOT_FOUND"
    OPERATION_DISABLED = "OPERATION_DISABLED"
    PRINCIPAL_REQUIRED = "PRINCIPAL_REQUIRED"
    PRINCIPAL_CONTEXT_MISMATCH = "PRINCIPAL_CONTEXT_MISMATCH"
    REQUIRED_SCOPE_MISSING = "REQUIRED_SCOPE_MISSING"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    DIRECT_REQUEST_REQUIRED = "DIRECT_REQUEST_REQUIRED"
    JUSTIFICATION_REQUIRED = "JUSTIFICATION_REQUIRED"
    JUSTIFICATION_TOO_SHORT = "JUSTIFICATION_TOO_SHORT"
    INVALID_ACTION_ARGUMENTS = "INVALID_ACTION_ARGUMENTS"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    CONFIRMATION_MISMATCH = "CONFIRMATION_MISMATCH"
    READ_APPROVED = "READ_APPROVED"
    WRITE_SIMULATION_APPROVED = "WRITE_SIMULATION_APPROVED"
    REAL_WRITE_DISABLED = "REAL_WRITE_DISABLED"


class AuthProfile(BaseModel):
    """Descreve como o executor obterá autenticação sem armazenar seu valor no YAML.

    ``env`` contém apenas o nome da variável de ambiente. ``context_field`` aponta para um valor
    do contexto validado da execução, como ``user_id`` no conector Tractian.
    """

    # ``extra='forbid'`` transforma campos desconhecidos em erro. Isso detecta cedo um typo como
    # ``contex_field``, que de outra forma poderia desativar autenticação silenciosamente.
    model_config = ConfigDict(extra="forbid")

    type: AuthType
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
    # Escopos ligam identidade, contexto e recurso. A igualdade entre as três fontes impede que
    # um agente troque, por exemplo, o ``company_id`` apenas alterando os argumentos da tool.
    required_scopes: list[str] = Field(default_factory=list)
    # JSON Pointer permite localizar a justificativa sem codificar o formato de uma API no Python.
    justification_pointer: str = "/justification"

    # Os limites impedem profiles acidentais com espera ou retries ilimitados.
    timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 10
    max_retries: Annotated[int, Field(ge=0, le=2)] = 0
    idempotent: bool = False

    # Campos listados aqui são removidos recursivamente de respostas e prévias de simulação.
    redact_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_policy_fields(self) -> "OperationPolicy":
        """Rejeita escopos ambíguos e pointers inválidos ao carregar o conector."""

        if len(self.required_scopes) != len(set(self.required_scopes)):
            raise ValueError("required_scopes não pode conter valores duplicados")
        if any(not scope or scope.strip() != scope for scope in self.required_scopes):
            raise ValueError("required_scopes deve conter nomes não vazios e sem espaços externos")
        if self.justification_pointer and not self.justification_pointer.startswith("/"):
            raise ValueError("justification_pointer deve ser um JSON Pointer iniciado por '/'")
        # RFC 6901 define somente ``~0`` e ``~1``. Rejeitar outros escapes no startup evita que
        # uma justificativa válida fique invisível por um typo de configuração.
        for token in self.justification_pointer.split("/")[1:]:
            index = 0
            while index < len(token):
                if token[index] == "~":
                    if index + 1 >= len(token) or token[index + 1] not in {"0", "1"}:
                        raise ValueError(
                            "justification_pointer contém escape JSON Pointer inválido"
                        )
                    index += 2
                    continue
                index += 1
        return self


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


class DomainIntent(BaseModel):
    """Intenção de negócio e operações que podem fundamentá-la ou realizá-la.

    O modelo escolhe somente o ``id`` da intenção. As listas de operações continuam vindo do
    arquivo validado, o que impede que uma resposta do LLM invente uma capacidade do conector.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    description: str = Field(min_length=1)
    evidence_operations: list[str] = Field(default_factory=list)
    action_operations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_operation_lists(self) -> "DomainIntent":
        """Mantém referências não vazias, únicas e sem dupla classificação."""

        for field_name, operations in (
            ("evidence_operations", self.evidence_operations),
            ("action_operations", self.action_operations),
        ):
            if len(operations) != len(set(operations)):
                raise ValueError(f"{field_name} não pode conter duplicatas")
            if any(not operation or operation.strip() != operation for operation in operations):
                raise ValueError(f"{field_name} contém operationId inválido")
        overlap = set(self.evidence_operations) & set(self.action_operations)
        if overlap:
            raise ValueError("uma operação não pode ser evidência e ação na mesma intenção")
        return self


class ConnectorDomain(BaseModel):
    """Vocabulário tipado usado pelo classificador e pelo planejador do agente."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    language: str = Field(default="pt-BR", min_length=2)
    context_fields: list[str] = Field(default_factory=list)
    terminology: dict[str, str] = Field(default_factory=dict)
    intents: list[DomainIntent] = Field(default_factory=list)
    evidence_states: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_domain_collections(self) -> "ConnectorDomain":
        """Rejeita ambiguidades que tornariam prompts e métricas não determinísticos."""

        if len(self.context_fields) != len(set(self.context_fields)):
            raise ValueError("context_fields não pode conter duplicatas")
        if any(not field or field.strip() != field for field in self.context_fields):
            raise ValueError("context_fields contém nome inválido")
        intent_ids = [intent.id for intent in self.intents]
        if len(intent_ids) != len(set(intent_ids)):
            raise ValueError("intents não pode conter ids duplicados")
        if len(self.evidence_states) != len(set(self.evidence_states)):
            raise ValueError("evidence_states não pode conter duplicatas")
        if any(not state or state.strip() != state for state in self.evidence_states):
            raise ValueError("evidence_states contém estado inválido")
        if any(
            not term or term.strip() != term or not definition.strip()
            for term, definition in self.terminology.items()
        ):
            raise ValueError("terminology contém termo ou definição inválida")
        return self


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
    required_scopes: list[str] = Field(default_factory=list)
    justification_pointer: str
    timeout_seconds: float
    max_retries: int
    idempotent: bool


class ConnectorSummary(BaseModel):
    """Resumo de conector usado em listagens, sem detalhes sensíveis de conexão."""

    id: str
    name: str
    description: str
    openapi_version: str
    auth_type: AuthType
    operation_count: int
    enabled_operation_count: int
    context_fields: list[str] = Field(default_factory=list)


class ConnectorDetails(ConnectorSummary):
    """Resumo acrescido das operações consolidadas do conector."""

    operations: list[OperationSummary]


class ExecutionArguments(BaseModel):
    """Argumentos separados por sua posição no request HTTP.

    Separar as posições evita ambiguidades. Um mesmo nome pode existir no path e na query, e um
    header não deve ser confundido com um campo livre que o executor inventaria. Cookie permanece
    fora da primeira versão e, por isso, continua sendo rejeitado como campo extra.
    """

    model_config = ConfigDict(extra="forbid")

    path: dict[str, Any] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, Any] = Field(default_factory=dict)
    # ``None`` pode ser um body JSON válido. O executor usa ``model_fields_set`` para distinguir
    # "body ausente" de "body enviado explicitamente como null".
    body: Any = None


class OperationExecutionRequest(BaseModel):
    """Pedido interno, independente de FastAPI, para executar uma operação do catálogo."""

    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    operation_id: str = Field(min_length=1)
    arguments: ExecutionArguments = Field(default_factory=ExecutionArguments)
    # O contexto será usado por autenticação derivada da pessoa e pela policy engine. Ele já faz
    # parte do contrato para que o executor não precise mudar de assinatura depois.
    context: dict[str, Any] = Field(default_factory=dict)


class PolicyPrincipal(BaseModel):
    """Identidade e autorizações obtidas de uma fonte confiável, nunca escolhidas pelo LLM."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    permissions: list[str] = Field(default_factory=list)
    scopes: dict[str, ScopeValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_authorization_claims(self) -> "PolicyPrincipal":
        """Evita claims duplicadas ou vazias que dificultariam auditoria."""

        if len(self.permissions) != len(set(self.permissions)):
            raise ValueError("permissions não pode conter valores duplicados")
        if any(
            not permission or permission.strip() != permission for permission in self.permissions
        ):
            raise ValueError("permissions deve conter nomes não vazios e sem espaços externos")
        if any(not scope or scope.strip() != scope for scope in self.scopes):
            raise ValueError("scopes deve conter nomes não vazios e sem espaços externos")
        return self


class PolicyConfirmation(BaseModel):
    """Confirmação explícita vinculada à pessoa e ao digest exato da ação."""

    model_config = ConfigDict(extra="forbid")

    confirmed_by: str = Field(min_length=1)
    action_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class PolicyEvaluationRequest(BaseModel):
    """Todos os sinais confiáveis necessários para avaliar uma execução."""

    model_config = ConfigDict(extra="forbid")

    execution: OperationExecutionRequest
    principal: PolicyPrincipal | None = None
    resource_scopes: dict[str, ScopeValue] = Field(default_factory=dict)
    direct_request: bool = False
    confirmation: PolicyConfirmation | None = None


class PolicyDecision(BaseModel):
    """Decisão explicável sem expor argumentos, credenciais ou claims em texto puro."""

    connector_id: str
    operation_id: str
    outcome: PolicyOutcome
    reason_codes: Annotated[list[PolicyReasonCode], Field(min_length=1)]
    access: AccessMode | None = None
    risk: RiskLevel | None = None
    required_permission: str | None = None
    required_scopes: list[str] = Field(default_factory=list)
    confirmation_required_for_execute: bool = False
    action_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    message: str


class ExecutionErrorDetails(BaseModel):
    """Erro estruturado que pode ser consumido igualmente por API, agente, trace e testes."""

    code: str
    message: str
    retryable: bool = False


class SimulatedAction(BaseModel):
    """Prévia segura de uma escrita que foi validada, mas não chegou à rede.

    A prévia usa somente o path relativo e nunca inclui valores de autenticação. ``body_present``
    diferencia uma operação sem body de outra que enviaria ``null`` explicitamente.
    """

    method: str
    path: str
    query: dict[str, Any] = Field(default_factory=dict)
    header_names: list[str] = Field(default_factory=list)
    body_present: bool = False
    body: Any = None
    auth_type: AuthType


class OperationExecutionResult(BaseModel):
    """Envelope comum devolvido independentemente da API conectada."""

    connector_id: str
    operation_id: str
    outcome: ExecutionOutcome
    status_code: Annotated[int, Field(ge=100, le=599)] | None = None
    data: Any = None
    error: ExecutionErrorDetails | None = None
    # Bloqueios e simulações não abrem conexão. Uma execução HTTP começa em uma tentativa.
    attempts: Annotated[int, Field(ge=0)] = 0
    simulation: SimulatedAction | None = None
    latency_ms: Annotated[float, Field(ge=0)]


class GuardedExecutionResult(BaseModel):
    """Une a decisão política ao resultado HTTP, que só existe quando ela autoriza o fluxo."""

    policy: PolicyDecision
    execution: OperationExecutionResult | None = None


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
    execution_mode: ExecutionMode
