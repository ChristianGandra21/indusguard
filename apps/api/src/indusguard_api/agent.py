"""Runtime interno do agente: LangGraph coordena, MCP executa e a policy decide.

Este módulo é deliberadamente uma interface interna, sem rota FastAPI. A entrada controlada pela
pessoa (``AgentRunRequest``) permanece separada do ``TrustedRunContext`` criado pelo host
autenticado. Essa divisão impede que o modelo invente identidade, permissões ou confirmações.

O grafo não conhece HTTP nem particularidades da Tractian. Ele descobre as tools do conector no
servidor MCP em memória e devolve um envelope comum, inclusive quando termina parcialmente.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from time import perf_counter
from typing import Any, Protocol
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from mcp import Client
from mcp_types import Tool
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing_extensions import TypedDict

from indusguard_api.connectors import ConnectorCatalog
from indusguard_api.mcp_server import (
    ProtectedOperationExecutor,
    TrustedPolicyContextProvider,
    TrustedPolicySignals,
    create_mcp_server,
)
from indusguard_api.observability import (
    NoOpTelemetry,
    Telemetry,
    TelemetrySnapshot,
    mark_span_error,
)
from indusguard_api.redaction import redact_text
from indusguard_api.schemas import (
    ConnectorDomain,
    PolicyConfirmation,
    PolicyPrincipal,
    ScopeValue,
)

PROMPT_VERSION = "agent-v1"
POLICY_VERSION = "policy-v1"


class AgentDecision(StrEnum):
    """Decisão de alto nível apresentada sem expor raciocínio interno do modelo."""

    ORIENT = "orient"
    ACT = "act"
    ESCALATE = "escalate"


class AgentRunStatus(StrEnum):
    """Distingue conclusão completa, útil parcial e falha sem evidência."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class AgentObservabilityStatus(StrEnum):
    """Saúde da auditoria sem confundir sua falha com o resultado funcional da run."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DISABLED = "disabled"


class ObservabilityComponentStatus(StrEnum):
    """Estado público e limitado de cada destino de observabilidade."""

    DISABLED = "disabled"
    CONFIGURED = "configured"
    RECORDED = "recorded"
    FAILED = "failed"


class AgentTerminationReason(StrEnum):
    """Códigos estáveis usados por testes, métricas e futura observabilidade."""

    COMPLETED = "COMPLETED"
    AMBIGUOUS_INTENT = "AMBIGUOUS_INTENT"
    MODEL_RATE_LIMITED = "MODEL_RATE_LIMITED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
    MODEL_TOOL_ERROR = "MODEL_TOOL_ERROR"
    MCP_ERROR = "MCP_ERROR"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    MAX_MODEL_CALLS = "MAX_MODEL_CALLS"
    MAX_TOOL_CALLS = "MAX_TOOL_CALLS"
    EVIDENCE_LIMIT = "EVIDENCE_LIMIT"
    TIMEOUT = "TIMEOUT"
    FINALIZATION_ERROR = "FINALIZATION_ERROR"


class AgentRunRequest(BaseModel):
    """Entrada controlada pela pessoa para uma execução única e sem memória."""

    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    message: str = Field(min_length=1, max_length=8000)
    seed: int = Field(default=0, ge=0, le=2_147_483_647)

    @model_validator(mode="after")
    def reject_blank_message(self) -> AgentRunRequest:
        """Espaços não formam uma solicitação útil, embora satisfaçam ``min_length``."""

        if not self.message.strip():
            raise ValueError("message não pode conter somente espaços")
        return self


class TrustedRunContext(BaseModel):
    """Sinais fornecidos pelo host autenticado, nunca pelos argumentos do modelo."""

    model_config = ConfigDict(extra="forbid")

    principal: PolicyPrincipal | None = None
    execution_context: dict[str, Any] = Field(default_factory=dict)
    resource_scopes: dict[str, ScopeValue] = Field(default_factory=dict)
    direct_request: bool = False
    confirmation: PolicyConfirmation | None = None


class AgentPlanningContext(BaseModel):
    """Recorte seguro do contexto confiável que pode orientar o modelo.

    O host continua sendo a autoridade sobre estes valores. A lista de campos de contexto vem do
    ``domain.yaml`` e os escopos usam a mesma allowlist, evitando enviar claims internas, segredos
    ou a confirmação vinculada a uma ação.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    context: dict[str, Any] = Field(default_factory=dict)
    permissions: list[str] = Field(default_factory=list)
    scopes: dict[str, ScopeValue] = Field(default_factory=dict)
    direct_request: bool = False


class AgentIntentDecision(BaseModel):
    """Saída estrita do classificador; ``None`` significa que falta clareza."""

    model_config = ConfigDict(extra="forbid")

    intent_id: str | None = None
    uncertainties: list[str] = Field(default_factory=list)


class AgentPlannedToolCall(BaseModel):
    """Chamada proposta pelo modelo usando alias seguro, não o nome MCP diretamente."""

    model_config = ConfigDict(extra="forbid")

    alias: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    arguments: dict[str, Any] = Field(default_factory=dict)
    call_id: str = Field(default_factory=lambda: f"call-{uuid4().hex}", min_length=1)


class AgentPlanStep(BaseModel):
    """Um turno de planejamento pode concluir ou pedir tools executadas sequencialmente."""

    model_config = ConfigDict(extra="forbid")

    tool_calls: list[AgentPlannedToolCall] = Field(default_factory=list)
    done: bool = False
    note: str | None = None

    @model_validator(mode="after")
    def validate_step(self) -> AgentPlanStep:
        """Evita um turno contraditório que conclui e solicita efeitos ao mesmo tempo."""

        if self.done and self.tool_calls:
            raise ValueError("um passo concluído não pode solicitar tools")
        if not self.done and not self.tool_calls:
            raise ValueError("um passo precisa concluir ou solicitar ao menos uma tool")
        return self


class AgentFinalAnswer(BaseModel):
    """Contrato do finalizador separado, chamado sem tools disponíveis."""

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1)
    decision: AgentDecision
    evidence_ids: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)


class AgentToolDefinition(BaseModel):
    """Tool MCP convertida para um nome aceito pelos modelos da Groq."""

    model_config = ConfigDict(extra="forbid")

    alias: str
    mcp_name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool
    destructive: bool
    idempotent: bool

    def as_model_tool(self) -> dict[str, Any]:
        """Produz o formato de function tool aceito por ``ChatGroq.bind_tools``."""

        return {
            "type": "function",
            "function": {
                "name": self.alias,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class TokenUsage(BaseModel):
    """Contagem normalizada entre o fake determinístico e respostas da Groq."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class GatewayResult[GatewayValue]:
    """Valor estruturado acompanhado do uso retornado pelo provedor.

    ``provider_message`` fica restrita ao histórico transitório da run. Adaptadores compatíveis com
    OpenAI podem precisar reenviar metadados opacos do turno anterior sem persisti-los no resultado.
    """

    value: GatewayValue
    usage: TokenUsage = field(default_factory=TokenUsage)
    provider_message: AIMessage | None = field(default=None, repr=False, compare=False)


class AgentModelError(RuntimeError):
    """Base para falhas redigidas do provedor de modelo."""


class ModelRateLimitedError(AgentModelError):
    """A faixa gratuita recusou a chamada por limite de uso."""

    def __init__(self, message: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = (
            retry_after_seconds
            if retry_after_seconds is None or 0 <= retry_after_seconds <= 86_400
            else None
        )


class ModelUnavailableError(AgentModelError):
    """O provedor não respondeu de forma utilizável."""

    def __init__(self, message: str, *, reason_code: str = "MODEL_UNAVAILABLE") -> None:
        super().__init__(message)
        self.reason_code = reason_code


class ModelOutputError(AgentModelError):
    """A resposta não correspondeu ao contrato estruturado esperado."""


class AgentModelGateway(Protocol):
    """Seam do único sistema probabilístico; testes substituem apenas esta fronteira."""

    @property
    def model_name(self) -> str:
        """Identificador registrável do modelo, sem credenciais."""

    async def classify(
        self,
        *,
        request: AgentRunRequest,
        domain: ConnectorDomain,
    ) -> GatewayResult[AgentIntentDecision]:
        """Escolhe uma intenção declarada ou devolve intenção ambígua."""

    async def plan(
        self,
        *,
        request: AgentRunRequest,
        domain: ConnectorDomain,
        intent: AgentIntentDecision,
        planning_context: AgentPlanningContext,
        messages: Sequence[BaseMessage],
        tools: Sequence[AgentToolDefinition],
    ) -> GatewayResult[AgentPlanStep]:
        """Propõe tools do conector ou encerra a coleta."""

    async def finalize(
        self,
        *,
        request: AgentRunRequest,
        domain: ConnectorDomain,
        intent: AgentIntentDecision,
        planning_context: AgentPlanningContext,
        messages: Sequence[BaseMessage],
        allowed_evidence_ids: Sequence[str],
    ) -> GatewayResult[AgentFinalAnswer]:
        """Produz resposta estrita sem acesso a tools."""


class ScriptedAgentModelGateway:
    """Modelo fake determinístico para CI; nunca realiza rede nem tenta imitar um LLM."""

    def __init__(
        self,
        *,
        classification: AgentIntentDecision | Exception,
        plans: Sequence[AgentPlanStep | Exception],
        final_answer: AgentFinalAnswer | Exception,
        usage: TokenUsage | None = None,
        model_name: str = "scripted-test-model",
        delay_seconds: float = 0,
    ) -> None:
        self._classification = classification
        self._plans = deque(plans)
        self._final_answer = final_answer
        self._usage = usage or TokenUsage()
        self._model_name = model_name
        self._delay_seconds = delay_seconds
        # Os registros permitem testar a fronteira do fake sem mockar nós internos do grafo.
        self.classify_requests: list[AgentRunRequest] = []
        self.plan_messages: list[list[BaseMessage]] = []
        self.finalize_messages: list[list[BaseMessage]] = []
        self.seen_tools: list[list[AgentToolDefinition]] = []
        self.seen_planning_contexts: list[AgentPlanningContext] = []

    @property
    def model_name(self) -> str:
        return self._model_name

    async def classify(
        self,
        *,
        request: AgentRunRequest,
        domain: ConnectorDomain,
    ) -> GatewayResult[AgentIntentDecision]:
        del domain
        self.classify_requests.append(request)
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        if isinstance(self._classification, Exception):
            raise self._classification
        return GatewayResult(self._classification, self._usage)

    async def plan(
        self,
        *,
        request: AgentRunRequest,
        domain: ConnectorDomain,
        intent: AgentIntentDecision,
        planning_context: AgentPlanningContext,
        messages: Sequence[BaseMessage],
        tools: Sequence[AgentToolDefinition],
    ) -> GatewayResult[AgentPlanStep]:
        del request, domain, intent
        self.plan_messages.append(list(messages))
        self.seen_tools.append(list(tools))
        self.seen_planning_contexts.append(planning_context)
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        if not self._plans:
            return GatewayResult(AgentPlanStep(done=True), self._usage)
        plan = self._plans.popleft()
        if isinstance(plan, Exception):
            raise plan
        return GatewayResult(plan, self._usage)

    async def finalize(
        self,
        *,
        request: AgentRunRequest,
        domain: ConnectorDomain,
        intent: AgentIntentDecision,
        planning_context: AgentPlanningContext,
        messages: Sequence[BaseMessage],
        allowed_evidence_ids: Sequence[str],
    ) -> GatewayResult[AgentFinalAnswer]:
        del request, domain, intent, planning_context, allowed_evidence_ids
        self.finalize_messages.append(list(messages))
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        if isinstance(self._final_answer, Exception):
            raise self._final_answer
        return GatewayResult(self._final_answer, self._usage)


class AgentEvidence(BaseModel):
    """Resultado MCP redigido e limitado que fundamenta a resposta final."""

    id: str
    tool_alias: str
    mcp_tool_name: str
    result: dict[str, Any]
    outcome: str
    status_code: int | None = None
    original_size_bytes: int = Field(ge=0)
    stored_size_bytes: int = Field(ge=0)
    truncated: bool = False


class AgentToolCall(BaseModel):
    """Registro seguro de uma proposta do modelo e de seu resultado observável."""

    tool_alias: str
    mcp_tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    evidence_id: str | None = None
    status: str
    outcome: str
    latency_ms: float = Field(ge=0)


class AgentRunMetrics(BaseModel):
    """Métricas mínimas para comparar comportamento sem armazenar raciocínio interno."""

    model: str
    prompt_version: str
    domain_version: str
    policy_version: str
    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    latency_ms: float = Field(ge=0)
    termination_reason: AgentTerminationReason
    retry_after_seconds: int | None = Field(default=None, ge=0, le=86_400)
    truncations: int = Field(ge=0)
    observability_degraded: bool = False


class AgentObservability(BaseModel):
    """Ressalva de auditoria apresentada em primeiro nível ao futuro frontend/API."""

    status: AgentObservabilityStatus = AgentObservabilityStatus.DISABLED
    warning_code: str | None = None
    message: str | None = None
    persistence: ObservabilityComponentStatus = ObservabilityComponentStatus.DISABLED
    local_trace: ObservabilityComponentStatus = ObservabilityComponentStatus.DISABLED
    otlp: ObservabilityComponentStatus = ObservabilityComponentStatus.DISABLED


class AgentRunResult(BaseModel):
    """Envelope stateless devolvido pelo runtime interno."""

    run_id: str
    started_at: datetime
    completed_at: datetime
    connector_id: str
    status: AgentRunStatus
    intent: AgentIntentDecision
    decision: AgentDecision
    answer: str
    evidence_ids: list[str]
    evidence: list[AgentEvidence]
    uncertainties: list[str]
    tool_calls: list[AgentToolCall]
    metrics: AgentRunMetrics
    observability: AgentObservability = Field(default_factory=AgentObservability)


class AgentRunRecorder(Protocol):
    """Seam de persistência: o grafo entrega um resultado seguro e nunca executa SQL."""

    async def record(self, *, request: AgentRunRequest, result: AgentRunResult) -> None:
        """Persiste uma run completa ou levanta uma falha redigida para o coordenador."""


class AgentRuntimeConfig(BaseModel):
    """Limites defensivos injetáveis para testes, deployment e futuras avaliações."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_model_calls: int = Field(default=8, ge=2, le=32)
    max_tool_calls: int = Field(default=12, ge=1, le=64)
    run_timeout_seconds: float = Field(default=60, gt=0, le=3600)
    max_evidence_bytes: int = Field(default=32 * 1024, ge=256, le=1024 * 1024)
    max_run_evidence_bytes: int = Field(default=128 * 1024, ge=256, le=4 * 1024 * 1024)
    prompt_version: str = PROMPT_VERSION
    policy_version: str = POLICY_VERSION


class AgentConfigurationError(ValueError):
    """Configuração impossível detectada antes de iniciar chamadas probabilísticas."""


class RunBoundTrustedContextProvider(TrustedPolicyContextProvider):
    """Adapta o contexto confiável de uma run para a interface exigida pelo MCP."""

    def __init__(self, context: TrustedRunContext) -> None:
        self._context = context

    async def resolve(self, **_: object) -> TrustedPolicySignals:
        return TrustedPolicySignals(
            principal=self._context.principal,
            execution_context=self._context.execution_context,
            resource_scopes=self._context.resource_scopes,
            direct_request=self._context.direct_request,
            confirmation=self._context.confirmation,
        )


@dataclass
class _RunData:
    run_id: str
    request: AgentRunRequest
    trusted_context: TrustedRunContext
    planning_context: AgentPlanningContext
    started_at: float
    started_at_utc: datetime
    domain: ConnectorDomain | None = None
    tools: list[AgentToolDefinition] = field(default_factory=list)
    tool_map: dict[str, AgentToolDefinition] = field(default_factory=dict)
    intent: AgentIntentDecision = field(default_factory=AgentIntentDecision)
    messages: list[BaseMessage] = field(default_factory=list)
    pending_calls: list[AgentPlannedToolCall] = field(default_factory=list)
    evidence: list[AgentEvidence] = field(default_factory=list)
    tool_calls: list[AgentToolCall] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    final_answer: AgentFinalAnswer | None = None
    termination: AgentTerminationReason = AgentTerminationReason.COMPLETED
    retry_after_seconds: int | None = None
    stop_planning: bool = False
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    evidence_bytes: int = 0
    truncations: int = 0


class _GraphState(TypedDict):
    data: _RunData
    step: str


_DEFAULT_REDACT_FIELDS = frozenset(
    {"api_key", "apikey", "authorization", "credential", "password", "secret", "token"}
)


def _redact_arguments(value: Any, fields: frozenset[str]) -> Any:
    """Remove recursivamente campos declarados no conector e nomes sensíveis universais."""

    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]" if str(key).lower() in fields else _redact_arguments(child, fields)
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_arguments(child, fields) for child in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _json_bytes(value: Any) -> bytes:
    """Serialização canônica usada somente para limites e hashes, nunca como prompt secreto."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _domain_version(domain: ConnectorDomain) -> str:
    """Hash reproduzível muda quando qualquer regra declarativa do domínio muda."""

    return hashlib.sha256(_json_bytes(domain.model_dump(mode="json"))).hexdigest()


def _add_uncertainty(data: _RunData, code: str) -> None:
    """Evita duplicar o mesmo aviso quando mais de um nó observa a mesma condição."""

    if code not in data.uncertainties:
        data.uncertainties.append(code)


def _find_evidence_states(value: Any, allowed: frozenset[str]) -> set[str]:
    """Localiza estados declarados apenas nos dados JSON, sem interpretar texto livre."""

    found: set[str] = set()
    if isinstance(value, Mapping):
        for child in value.values():
            found.update(_find_evidence_states(child, allowed))
    elif isinstance(value, list):
        for child in value:
            found.update(_find_evidence_states(child, allowed))
    elif isinstance(value, str) and value in allowed:
        found.add(value)
    return found


def _usage(data: _RunData, result: GatewayResult[Any]) -> None:
    data.input_tokens += result.usage.input_tokens
    data.output_tokens += result.usage.output_tokens


def _termination_for_error(error: AgentModelError) -> AgentTerminationReason:
    if isinstance(error, ModelRateLimitedError):
        return AgentTerminationReason.MODEL_RATE_LIMITED
    if isinstance(error, ModelOutputError):
        return AgentTerminationReason.MODEL_OUTPUT_INVALID
    return AgentTerminationReason.MODEL_UNAVAILABLE


def _capture_rate_limit(data: _RunData, error: AgentModelError) -> None:
    """Propaga somente a espera redigida; headers e corpo do provedor ficam na borda."""

    if isinstance(error, ModelRateLimitedError):
        data.retry_after_seconds = error.retry_after_seconds


def _capture_model_failure(data: _RunData, error: AgentModelError) -> None:
    """Registra apenas a classe estável da falha, sem copiar detalhes do provedor."""

    _capture_rate_limit(data, error)
    if isinstance(error, ModelUnavailableError):
        _add_uncertainty(data, error.reason_code)


def _bounded_result(
    payload: Mapping[str, Any],
    *,
    per_evidence_limit: int,
    remaining_run_bytes: int,
) -> tuple[dict[str, Any], int, int, bool]:
    """Limita dados grandes preservando decisão, status e erro necessários para segurança."""

    normalized = dict(payload)
    original_size = len(_json_bytes(normalized))
    allowed = min(per_evidence_limit, max(remaining_run_bytes, 0))
    if original_size <= allowed:
        return normalized, original_size, original_size, False

    policy = normalized.get("policy")
    if isinstance(policy, Mapping):
        compact_policy: Any = {
            key: policy.get(key)
            for key in (
                "connector_id",
                "operation_id",
                "outcome",
                "reason_codes",
                "confirmation_required_for_execute",
                "action_digest",
            )
            if key in policy
        }
    else:
        compact_policy = policy
    execution = normalized.get("execution")
    if isinstance(execution, Mapping):
        compact_execution = {
            key: execution.get(key)
            for key in (
                "connector_id",
                "operation_id",
                "outcome",
                "status_code",
                "error",
                "attempts",
                "simulation",
                "latency_ms",
            )
            if key in execution
        }
        compact_execution["data"] = {
            "truncated": True,
            "original_size_bytes": original_size,
        }
    else:
        compact_execution = execution
    compact: dict[str, Any] = {
        "policy": compact_policy,
        "execution": compact_execution,
        "truncation": {
            "reason": "EVIDENCE_SIZE_LIMIT",
            "original_size_bytes": original_size,
        },
    }
    compact_size = len(_json_bytes(compact))
    if compact_size > allowed:
        compact = {
            "truncation": {
                "reason": "RUN_EVIDENCE_SIZE_LIMIT",
                "original_size_bytes": original_size,
            }
        }
        compact_size = len(_json_bytes(compact))
    # Mesmo com orçamento esgotado mantemos um marcador pequeno e contabilizamos seu tamanho.
    return compact, original_size, compact_size, True


class AgentRuntime:
    """Orquestra uma run isolada sem expor HTTP, policy ou contexto ao modelo."""

    def __init__(
        self,
        catalog: ConnectorCatalog,
        operation_executor: ProtectedOperationExecutor,
        model_gateway: AgentModelGateway,
        config: AgentRuntimeConfig | None = None,
        *,
        recorder: AgentRunRecorder | None = None,
        telemetry: Telemetry | None = None,
    ) -> None:
        self._catalog = catalog
        self._operation_executor = operation_executor
        self._model = model_gateway
        self._config = config or AgentRuntimeConfig()
        self._recorder = recorder
        self._telemetry = telemetry or NoOpTelemetry()

    async def run(
        self,
        request: AgentRunRequest,
        trusted_context: TrustedRunContext,
    ) -> AgentRunResult:
        """Executa o grafo dentro do prazo e sempre fecha o cliente MCP em memória."""

        if self._catalog.get(request.connector_id) is None:
            raise AgentConfigurationError(
                f"conector '{request.connector_id}' não existe no catálogo carregado"
            )
        domain = self._catalog.get_domain(request.connector_id)
        if domain is None:
            raise AgentConfigurationError(
                f"conector '{request.connector_id}' não possui domain.yaml para o agente"
            )
        if domain.id != request.connector_id:
            raise AgentConfigurationError("domain.id não corresponde ao conector selecionado")

        run_id = str(uuid4())
        started_at = perf_counter()
        started_at_utc = datetime.now(UTC)
        data = _RunData(
            run_id=run_id,
            request=request,
            trusted_context=trusted_context,
            planning_context=self._planning_context(domain, trusted_context),
            started_at=started_at,
            started_at_utc=started_at_utc,
            domain=domain,
            messages=[HumanMessage(content=request.message)],
        )
        provider = RunBoundTrustedContextProvider(trusted_context)
        server = create_mcp_server(self._catalog, self._operation_executor, provider)

        with self._telemetry.start_span(
            "indusguard.agent.run",
            {
                "indusguard.run.id": run_id,
                "indusguard.connector.id": request.connector_id,
                "gen_ai.request.model": self._model.model_name,
                "indusguard.prompt.version": self._config.prompt_version,
                "indusguard.policy.version": self._config.policy_version,
            },
        ) as run_span:
            try:
                async with asyncio.timeout(self._config.run_timeout_seconds):
                    # O modo ``auto`` usa o dispatch direto oficial para servidores em processo.
                    # O modo legacy continua coberto pelos testes do adaptador MCP, mas depende de
                    # uma sessão JSON-RPC longa que não deve atravessar os tasks internos do grafo.
                    async with Client(server, mode="auto") as client:
                        graph = self._build_graph(client, data)
                        await graph.ainvoke({"data": data, "step": "start"})
            except TimeoutError:
                data.termination = AgentTerminationReason.TIMEOUT
                data.stop_planning = True
                _add_uncertainty(data, AgentTerminationReason.TIMEOUT)

            result = self._result(data)
            result = await self._record_result(request, result)
            run_span.set_attribute("indusguard.run.status", result.status.value)
            run_span.set_attribute("indusguard.run.decision", result.decision.value)
            run_span.set_attribute(
                "indusguard.run.termination_reason",
                result.metrics.termination_reason.value,
            )
            run_span.set_attribute("gen_ai.usage.input_tokens", result.metrics.input_tokens)
            run_span.set_attribute("gen_ai.usage.output_tokens", result.metrics.output_tokens)
            run_span.set_attribute("indusguard.run.tool_calls", result.metrics.tool_calls)
            run_span.set_attribute(
                "indusguard.observability.degraded",
                result.metrics.observability_degraded,
            )
            if result.status is AgentRunStatus.FAILED:
                mark_span_error(run_span, result.metrics.termination_reason.value)

        # O exporter JSONL é síncrono: ao fechar o span raiz já sabemos se esta própria escrita
        # falhou e conseguimos destacar a ressalva antes de devolver o resultado.
        return self._apply_observability(
            result,
            persistence=result.observability.persistence,
            telemetry=self._telemetry.snapshot(),
        )

    async def _record_result(
        self,
        request: AgentRunRequest,
        result: AgentRunResult,
    ) -> AgentRunResult:
        """Persiste sem transformar indisponibilidade de auditoria em falha do agente."""

        snapshot = self._telemetry.snapshot()
        if self._recorder is None:
            return self._apply_observability(
                result,
                persistence=ObservabilityComponentStatus.DISABLED,
                telemetry=snapshot,
            )

        optimistic = self._apply_observability(
            result,
            persistence=ObservabilityComponentStatus.RECORDED,
            telemetry=snapshot,
        )
        with self._telemetry.start_span(
            "indusguard.persistence.save",
            {"indusguard.run.id": result.run_id},
        ) as span:
            try:
                await self._recorder.record(request=request, result=optimistic)
            except Exception:
                # O erro técnico não é devolvido nem anexado ao span, evitando vazar DSN, SQL ou
                # conteúdo. O código estável é suficiente para métricas e para a futura UI.
                mark_span_error(span, "OBSERVABILITY_DEGRADED")
                return self._apply_observability(
                    result,
                    persistence=ObservabilityComponentStatus.FAILED,
                    telemetry=self._telemetry.snapshot(),
                )
        return optimistic

    @staticmethod
    def _apply_observability(
        result: AgentRunResult,
        *,
        persistence: ObservabilityComponentStatus,
        telemetry: TelemetrySnapshot,
    ) -> AgentRunResult:
        component = {
            "disabled": ObservabilityComponentStatus.DISABLED,
            "configured": ObservabilityComponentStatus.CONFIGURED,
            "recorded": ObservabilityComponentStatus.RECORDED,
            "failed": ObservabilityComponentStatus.FAILED,
        }
        local_trace = component[telemetry.local_trace]
        otlp = component[telemetry.otlp]
        degraded = (
            persistence is ObservabilityComponentStatus.FAILED
            or local_trace is ObservabilityComponentStatus.FAILED
            or otlp is ObservabilityComponentStatus.FAILED
        )
        enabled = persistence is not ObservabilityComponentStatus.DISABLED or telemetry.enabled
        status = (
            AgentObservabilityStatus.DEGRADED
            if degraded
            else AgentObservabilityStatus.HEALTHY
            if enabled
            else AgentObservabilityStatus.DISABLED
        )
        observability = AgentObservability(
            status=status,
            warning_code="OBSERVABILITY_DEGRADED" if degraded else None,
            message=(
                "A resposta foi produzida, mas parte do registro de auditoria não pôde ser salva."
                if degraded
                else None
            ),
            persistence=persistence,
            local_trace=local_trace,
            otlp=otlp,
        )
        metrics = result.metrics.model_copy(update={"observability_degraded": degraded})
        uncertainties = list(result.uncertainties)
        if degraded and "OBSERVABILITY_DEGRADED" not in uncertainties:
            uncertainties.append("OBSERVABILITY_DEGRADED")
        return result.model_copy(
            update={
                "observability": observability,
                "metrics": metrics,
                "uncertainties": uncertainties,
            }
        )

    def _build_graph(self, client: Any, data: _RunData) -> Any:
        """Compila um StateGraph explícito por run para manter o cliente MCP na mesma sessão."""

        async def validate_node(_: _GraphState) -> dict[str, Any]:
            listed = await client.list_tools()
            data.tools = self._tools_for_connector(listed.tools, data.request.connector_id)
            data.tool_map = {tool.alias: tool for tool in data.tools}
            if not data.tools:
                raise AgentConfigurationError(
                    f"conector '{data.request.connector_id}' não possui tools MCP habilitadas"
                )
            return {"data": data, "step": "validated"}

        async def classify_node(_: _GraphState) -> dict[str, Any]:
            with self._telemetry.start_span(
                "indusguard.model.classify",
                {
                    "indusguard.run.id": data.run_id,
                    "gen_ai.request.model": self._model.model_name,
                    "indusguard.model.call_index": data.model_calls + 1,
                },
            ) as span:
                try:
                    data.model_calls += 1
                    result = await self._model.classify(request=data.request, domain=data.domain)
                    _usage(data, result)
                    span.set_attribute("gen_ai.usage.input_tokens", result.usage.input_tokens)
                    span.set_attribute("gen_ai.usage.output_tokens", result.usage.output_tokens)
                    data.intent = AgentIntentDecision.model_validate(result.value)
                except AgentModelError as exc:
                    data.termination = _termination_for_error(exc)
                    _capture_model_failure(data, exc)
                    data.stop_planning = True
                    _add_uncertainty(data, data.termination)
                    mark_span_error(span, data.termination.value)
                    return {"data": data, "step": "classification_failed"}
                except Exception:
                    data.termination = AgentTerminationReason.MODEL_OUTPUT_INVALID
                    data.stop_planning = True
                    _add_uncertainty(data, data.termination)
                    mark_span_error(span, data.termination.value)
                    return {"data": data, "step": "classification_failed"}

                span.set_attribute(
                    "indusguard.intent.id",
                    data.intent.intent_id or "ambiguous",
                )

            valid_intents = {intent.id for intent in data.domain.intents}
            if data.intent.intent_id not in valid_intents:
                data.intent = AgentIntentDecision(
                    intent_id=None,
                    uncertainties=[*data.intent.uncertainties, "INTENT_NOT_IN_DOMAIN"],
                )
                data.termination = AgentTerminationReason.AMBIGUOUS_INTENT
                data.stop_planning = True
            else:
                data.tools = self._tools_for_intent(
                    data.tools,
                    data.domain,
                    data.intent.intent_id,
                )
                data.tool_map = {tool.alias: tool for tool in data.tools}
            for uncertainty in data.intent.uncertainties:
                _add_uncertainty(data, uncertainty)
            return {"data": data, "step": "classified"}

        async def plan_node(_: _GraphState) -> dict[str, Any]:
            # Sempre reserva uma chamada para o finalizador estruturado.
            if data.model_calls >= self._config.max_model_calls - 1:
                data.termination = AgentTerminationReason.MAX_MODEL_CALLS
                data.stop_planning = True
                _add_uncertainty(data, data.termination)
                return {"data": data, "step": "model_limit"}
            with self._telemetry.start_span(
                "indusguard.model.plan",
                {
                    "indusguard.run.id": data.run_id,
                    "gen_ai.request.model": self._model.model_name,
                    "indusguard.model.call_index": data.model_calls + 1,
                    "indusguard.model.available_tools": len(data.tools),
                },
            ) as span:
                try:
                    data.model_calls += 1
                    result = await self._model.plan(
                        request=data.request,
                        domain=data.domain,
                        intent=data.intent,
                        planning_context=data.planning_context,
                        messages=tuple(data.messages),
                        tools=tuple(data.tools),
                    )
                    _usage(data, result)
                    span.set_attribute("gen_ai.usage.input_tokens", result.usage.input_tokens)
                    span.set_attribute("gen_ai.usage.output_tokens", result.usage.output_tokens)
                    step = AgentPlanStep.model_validate(result.value)
                except AgentModelError as exc:
                    data.termination = _termination_for_error(exc)
                    _capture_model_failure(data, exc)
                    data.stop_planning = True
                    _add_uncertainty(data, data.termination)
                    mark_span_error(span, data.termination.value)
                    return {"data": data, "step": "planning_failed"}
                except Exception:
                    data.termination = AgentTerminationReason.MODEL_OUTPUT_INVALID
                    data.stop_planning = True
                    _add_uncertainty(data, data.termination)
                    mark_span_error(span, data.termination.value)
                    return {"data": data, "step": "planning_failed"}

                span.set_attribute("indusguard.model.planned_tools", len(step.tool_calls))
                span.set_attribute("indusguard.model.done", step.done)

            data.pending_calls = list(step.tool_calls)
            data.messages.append(
                result.provider_message
                if result.provider_message is not None
                else AIMessage(
                    content=step.note or "",
                    tool_calls=[
                        {
                            "name": call.alias,
                            "args": call.arguments,
                            "id": call.call_id,
                            "type": "tool_call",
                        }
                        for call in step.tool_calls
                    ],
                )
            )
            if step.done:
                data.stop_planning = True
            return {"data": data, "step": "planned"}

        async def tools_node(_: _GraphState) -> dict[str, Any]:
            for planned in data.pending_calls:
                if len(data.tool_calls) >= self._config.max_tool_calls:
                    data.termination = AgentTerminationReason.MAX_TOOL_CALLS
                    data.stop_planning = True
                    _add_uncertainty(data, data.termination)
                    break
                remaining_evidence_bytes = self._config.max_run_evidence_bytes - data.evidence_bytes
                # Um marcador de truncamento também precisa de espaço. Parar antes da call evita
                # obter dados externos que não poderiam ser representados no resultado da run.
                if remaining_evidence_bytes < 256:
                    data.termination = AgentTerminationReason.EVIDENCE_LIMIT
                    data.stop_planning = True
                    _add_uncertainty(data, data.termination)
                    break
                await self._execute_tool(client, data, planned)
            data.pending_calls = []
            return {"data": data, "step": "tools_executed"}

        async def finalize_node(_: _GraphState) -> dict[str, Any]:
            # Cota/indisponibilidade normalmente afetaria uma nova chamada também. Nesses casos
            # entregamos o fallback local em vez de insistir e consumir mais quota.
            skip_model = data.termination in {
                AgentTerminationReason.MODEL_RATE_LIMITED,
                AgentTerminationReason.MODEL_UNAVAILABLE,
                AgentTerminationReason.MODEL_OUTPUT_INVALID,
                AgentTerminationReason.TIMEOUT,
            }
            if skip_model or data.model_calls >= self._config.max_model_calls:
                return {"data": data, "step": "fallback_finalized"}
            with self._telemetry.start_span(
                "indusguard.model.finalize",
                {
                    "indusguard.run.id": data.run_id,
                    "gen_ai.request.model": self._model.model_name,
                    "indusguard.model.call_index": data.model_calls + 1,
                    "indusguard.evidence.count": len(data.evidence),
                },
            ) as span:
                try:
                    data.model_calls += 1
                    result = await self._model.finalize(
                        request=data.request,
                        domain=data.domain,
                        intent=data.intent,
                        planning_context=data.planning_context,
                        messages=tuple(data.messages),
                        allowed_evidence_ids=tuple(evidence.id for evidence in data.evidence),
                    )
                    _usage(data, result)
                    span.set_attribute("gen_ai.usage.input_tokens", result.usage.input_tokens)
                    span.set_attribute("gen_ai.usage.output_tokens", result.usage.output_tokens)
                    final_answer = AgentFinalAnswer.model_validate(result.value)
                    allowed = {evidence.id for evidence in data.evidence}
                    if not set(final_answer.evidence_ids).issubset(allowed):
                        data.termination = AgentTerminationReason.FINALIZATION_ERROR
                        _add_uncertainty(data, "FINALIZER_EVIDENCE_REFERENCE_INVALID")
                        mark_span_error(span, "FINALIZER_EVIDENCE_REFERENCE_INVALID")
                    else:
                        data.final_answer = final_answer
                        span.set_attribute(
                            "indusguard.run.decision",
                            final_answer.decision.value,
                        )
                        span.set_attribute(
                            "indusguard.evidence.references",
                            len(final_answer.evidence_ids),
                        )
                        for uncertainty in final_answer.uncertainties:
                            _add_uncertainty(data, uncertainty)
                except AgentModelError as exc:
                    data.termination = _termination_for_error(exc)
                    _capture_model_failure(data, exc)
                    _add_uncertainty(data, data.termination)
                    mark_span_error(span, data.termination.value)
                except Exception:
                    data.termination = AgentTerminationReason.FINALIZATION_ERROR
                    _add_uncertainty(data, data.termination)
                    mark_span_error(span, data.termination.value)
            return {"data": data, "step": "finalized"}

        async def after_classify(_: _GraphState) -> str:
            return "finalize" if data.stop_planning else "plan"

        async def after_plan(_: _GraphState) -> str:
            if data.pending_calls:
                return "tools"
            return "finalize"

        async def after_tools(_: _GraphState) -> str:
            return "finalize" if data.stop_planning else "plan"

        builder = StateGraph(_GraphState)
        builder.add_node("validate", validate_node)
        builder.add_node("classify", classify_node)
        builder.add_node("plan", plan_node)
        builder.add_node("tools", tools_node)
        builder.add_node("finalize", finalize_node)
        builder.add_edge(START, "validate")
        builder.add_edge("validate", "classify")
        builder.add_conditional_edges(
            "classify",
            after_classify,
            {"plan": "plan", "finalize": "finalize"},
        )
        builder.add_conditional_edges(
            "plan",
            after_plan,
            {"tools": "tools", "finalize": "finalize"},
        )
        builder.add_conditional_edges(
            "tools",
            after_tools,
            {"plan": "plan", "finalize": "finalize"},
        )
        builder.add_edge("finalize", END)
        return builder.compile()

    async def _execute_tool(
        self,
        client: Any,
        data: _RunData,
        planned: AgentPlannedToolCall,
    ) -> None:
        """Cria o span da tool sem expor argumentos e delega a normalização existente."""

        before_calls = len(data.tool_calls)
        with self._telemetry.start_span(
            "indusguard.tool.call",
            {
                "indusguard.run.id": data.run_id,
                "indusguard.tool.alias": planned.alias,
                "indusguard.tool.sequence": before_calls + 1,
            },
        ) as span:
            await self._execute_tool_untraced(client, data, planned)
            if len(data.tool_calls) == before_calls:
                mark_span_error(span, "MCP_CALL_NOT_RECORDED")
                return
            recorded = data.tool_calls[-1]
            span.set_attribute("indusguard.tool.status", recorded.status)
            span.set_attribute("indusguard.tool.outcome", recorded.outcome)
            span.set_attribute("indusguard.tool.latency_ms", recorded.latency_ms)
            if recorded.mcp_tool_name:
                span.set_attribute("indusguard.tool.name", recorded.mcp_tool_name)
            if recorded.evidence_id:
                span.set_attribute("indusguard.evidence.id", recorded.evidence_id)
            if recorded.status == "error":
                mark_span_error(span, recorded.outcome)

    async def _execute_tool_untraced(
        self,
        client: Any,
        data: _RunData,
        planned: AgentPlannedToolCall,
    ) -> None:
        """Resolve alias internamente e registra resultado MCP como evidência não confiável."""

        started = perf_counter()
        definition = data.tool_map.get(planned.alias)
        if definition is None:
            latency = (perf_counter() - started) * 1000
            data.tool_calls.append(
                AgentToolCall(
                    tool_alias=planned.alias,
                    arguments={},
                    status="error",
                    outcome="MODEL_TOOL_NOT_FOUND",
                    latency_ms=latency,
                )
            )
            data.messages.append(
                ToolMessage(
                    content=json.dumps(
                        {"code": "MODEL_TOOL_NOT_FOUND", "message": "Tool não disponível."},
                        ensure_ascii=False,
                    ),
                    tool_call_id=planned.call_id,
                    name=planned.alias,
                )
            )
            data.termination = AgentTerminationReason.MODEL_TOOL_ERROR
            _add_uncertainty(data, "MODEL_TOOL_NOT_FOUND")
            return

        try:
            result = await client.call_tool(definition.mcp_name, planned.arguments)
            raw_payload = result.structured_content
            payload = (
                dict(raw_payload)
                if isinstance(raw_payload, Mapping)
                else {
                    "code": "MCP_RESULT_INVALID",
                    "message": "A tool não retornou conteúdo estruturado.",
                }
            )
            is_error = bool(result.is_error)
        except Exception:
            payload = {
                "code": "MCP_CALL_FAILED",
                "message": "A chamada MCP não pôde ser concluída.",
            }
            is_error = True

        bounded, original_size, stored_size, truncated = _bounded_result(
            payload,
            per_evidence_limit=self._config.max_evidence_bytes,
            remaining_run_bytes=self._config.max_run_evidence_bytes - data.evidence_bytes,
        )
        data.evidence_bytes += stored_size
        evidence_id = f"ev-{len(data.evidence) + 1:03d}"
        policy = payload.get("policy") if isinstance(payload.get("policy"), Mapping) else {}
        execution = (
            payload.get("execution") if isinstance(payload.get("execution"), Mapping) else {}
        )
        outcome = str(
            execution.get("outcome") or policy.get("outcome") or payload.get("code") or "unknown"
        )
        evidence = AgentEvidence(
            id=evidence_id,
            tool_alias=planned.alias,
            mcp_tool_name=definition.mcp_name,
            result=bounded,
            outcome=outcome,
            status_code=execution.get("status_code"),
            original_size_bytes=original_size,
            stored_size_bytes=stored_size,
            truncated=truncated,
        )
        data.evidence.append(evidence)
        if truncated:
            data.truncations += 1
            _add_uncertainty(data, "EVIDENCE_TRUNCATED")
        if is_error:
            data.termination = AgentTerminationReason.MCP_ERROR
            _add_uncertainty(data, "MCP_TOOL_ERROR")
        elif execution.get("outcome") == "failed":
            # A chamada MCP funcionou e a policy autorizou, mas o sistema externo falhou.
            # Manter uma categoria própria evita confundir indisponibilidade com bloqueio.
            data.termination = AgentTerminationReason.UPSTREAM_ERROR
            _add_uncertainty(data, "UPSTREAM_ERROR")
        else:
            declared_states = frozenset(data.domain.evidence_states)
            for state in sorted(
                _find_evidence_states(execution.get("data"), declared_states) - {"complete"}
            ):
                _add_uncertainty(data, f"EVIDENCE_STATE_{state.upper()}")

        resolved = self._catalog.resolve_operation(
            data.request.connector_id,
            definition.mcp_name.split(".", 1)[1],
        )
        declared_fields = (
            frozenset(
                field.lower()
                for field in resolved.profile.operations[
                    resolved.operation.operation_id
                ].redact_fields
            )
            if resolved
            else frozenset()
        )
        arguments = _redact_arguments(
            planned.arguments,
            declared_fields | _DEFAULT_REDACT_FIELDS,
        )
        latency = (perf_counter() - started) * 1000
        data.tool_calls.append(
            AgentToolCall(
                tool_alias=planned.alias,
                mcp_tool_name=definition.mcp_name,
                arguments=arguments,
                evidence_id=evidence_id,
                status="error" if is_error else "completed",
                outcome=outcome,
                latency_ms=latency,
            )
        )
        data.messages.append(
            ToolMessage(
                content=json.dumps(bounded, ensure_ascii=False, sort_keys=True),
                tool_call_id=planned.call_id,
                name=planned.alias,
            )
        )

    def _tools_for_connector(
        self,
        tools: Sequence[Tool],
        connector_id: str,
    ) -> list[AgentToolDefinition]:
        """Filtra a descoberta MCP e cria aliases sem expor tools de outro conector."""

        prefix = f"{connector_id}."
        selected: list[AgentToolDefinition] = []
        aliases: set[str] = set()
        for tool in tools:
            if not tool.name.startswith(prefix):
                continue
            operation_id = tool.name[len(prefix) :]
            alias = f"{connector_id}__{operation_id}"
            if alias in aliases:
                raise AgentConfigurationError(f"colisão de alias de tool: '{alias}'")
            aliases.add(alias)
            annotations = tool.annotations
            resolved = self._catalog.resolve_operation(connector_id, operation_id)
            if resolved is None:
                raise AgentConfigurationError(
                    f"tool MCP referencia operação ausente: '{tool.name}'"
                )
            operation = resolved.operation
            policy_guidance = (
                "Política confiável: "
                f"access={operation.access.value}; "
                f"risk={operation.risk.value}; "
                f"permission={operation.permission or 'none'}; "
                f"direct_request={str(operation.requires_direct_request).lower()}; "
                f"justification_min_length={operation.justification_min_length}; "
                f"required_scopes={','.join(operation.required_scopes) or 'none'}; "
                f"confirmation={str(operation.requires_confirmation).lower()}."
            )
            description = tool.description or tool.title or operation_id
            selected.append(
                AgentToolDefinition(
                    alias=alias,
                    mcp_name=tool.name,
                    description=f"{description}\n{policy_guidance}",
                    input_schema=dict(tool.input_schema),
                    read_only=bool(annotations and annotations.read_only_hint),
                    destructive=bool(annotations and annotations.destructive_hint),
                    idempotent=bool(annotations and annotations.idempotent_hint),
                )
            )
        return sorted(selected, key=lambda tool: tool.alias)

    @staticmethod
    def _tools_for_intent(
        tools: Sequence[AgentToolDefinition],
        domain: ConnectorDomain,
        intent_id: str | None,
    ) -> list[AgentToolDefinition]:
        """Publica ao modelo somente operações da intenção classificada."""

        selected_intent = next(
            (intent for intent in domain.intents if intent.id == intent_id),
            None,
        )
        if selected_intent is None:
            return list(tools)
        allowed_operations = [
            *selected_intent.evidence_operations,
            *selected_intent.action_operations,
        ]
        tool_by_operation = {
            tool.alias.split("__", 1)[1] if "__" in tool.alias else tool.alias: tool
            for tool in tools
        }
        return [
            tool_by_operation[operation_id]
            for operation_id in dict.fromkeys(allowed_operations)
            if operation_id in tool_by_operation
        ]

    @staticmethod
    def _planning_context(
        domain: ConnectorDomain,
        trusted_context: TrustedRunContext,
    ) -> AgentPlanningContext:
        """Aplica a allowlist do domínio antes de qualquer mensagem ao modelo."""

        allowed = set(domain.context_fields)
        principal = trusted_context.principal
        context = {
            key: value for key, value in trusted_context.execution_context.items() if key in allowed
        }
        scopes = (
            {key: value for key, value in principal.scopes.items() if key in allowed}
            if principal
            else {}
        )
        result = AgentPlanningContext(
            context=context,
            permissions=sorted(principal.permissions) if principal else [],
            scopes=scopes,
            direct_request=trusted_context.direct_request,
        )
        try:
            json.dumps(result.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise AgentConfigurationError(
                "o contexto confiável permitido pelo domínio precisa ser JSON"
            ) from exc
        return result

    def _result(self, data: _RunData) -> AgentRunResult:
        """Converte estado parcial ou completo no mesmo contrato estável."""

        if data.final_answer is not None:
            answer = data.final_answer.answer
            decision = data.final_answer.decision
            evidence_ids = data.final_answer.evidence_ids
        else:
            evidence_ids = [evidence.id for evidence in data.evidence]
            if evidence_ids:
                answer = (
                    "A execução terminou parcialmente. Consulte as evidências estruturadas "
                    f"coletadas: {', '.join(evidence_ids)}."
                )
            else:
                answer = "Não foi possível concluir a solicitação de forma segura."
            decision = AgentDecision.ESCALATE

        if data.final_answer is not None and data.termination in {
            AgentTerminationReason.COMPLETED,
            AgentTerminationReason.AMBIGUOUS_INTENT,
        }:
            status = AgentRunStatus.COMPLETED
        elif data.evidence or data.tool_calls:
            status = AgentRunStatus.PARTIAL
        else:
            status = AgentRunStatus.FAILED

        latency_ms = (perf_counter() - data.started_at) * 1000
        metrics = AgentRunMetrics(
            model=self._model.model_name,
            prompt_version=self._config.prompt_version,
            domain_version=_domain_version(data.domain),
            policy_version=self._config.policy_version,
            model_calls=data.model_calls,
            tool_calls=len(data.tool_calls),
            input_tokens=data.input_tokens,
            output_tokens=data.output_tokens,
            total_tokens=data.input_tokens + data.output_tokens,
            latency_ms=latency_ms,
            termination_reason=data.termination,
            retry_after_seconds=data.retry_after_seconds,
            truncations=data.truncations,
        )
        return AgentRunResult(
            run_id=data.run_id,
            started_at=data.started_at_utc,
            completed_at=datetime.now(UTC),
            connector_id=data.request.connector_id,
            status=status,
            intent=data.intent,
            decision=decision,
            answer=answer,
            evidence_ids=evidence_ids,
            evidence=data.evidence,
            uncertainties=data.uncertainties,
            tool_calls=data.tool_calls,
            metrics=metrics,
        )
