"""Host público profundo para uma única execução autenticada do agente.

A rota HTTP conhece somente esta interface. Autenticação, admissão, contexto confiável,
LangGraph, MCP, policy e projeção ficam escondidos aqui para que nenhum novo endpoint consiga
contornar a mesma fronteira por conveniência.
"""

from __future__ import annotations

import asyncio
import math
import secrets
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from indusguard_api.agent import AgentRunRequest, AgentRunResult, TrustedRunContext
from indusguard_api.connectors import ConnectorCatalog
from indusguard_api.observability import NoOpTelemetry, Telemetry
from indusguard_api.persistence import Base, PublicRunQuotaRow, normalize_database_url
from indusguard_api.redaction import redact_text, redact_value
from indusguard_api.schemas import PolicyPrincipal, ScopeValue


class PublicRunErrorCode(StrEnum):
    """Falhas estáveis que a UI pode tratar sem inspecionar texto livre."""

    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_INVALID = "AUTH_INVALID"
    PUBLIC_RUNS_DISABLED = "PUBLIC_RUNS_DISABLED"
    CONNECTOR_NOT_PUBLIC = "CONNECTOR_NOT_PUBLIC"
    CONTEXT_INVALID = "CONTEXT_INVALID"
    RUN_RATE_LIMITED = "RUN_RATE_LIMITED"
    RUN_CONCURRENCY_LIMIT = "RUN_CONCURRENCY_LIMIT"
    MODEL_NOT_CONFIGURED = "MODEL_NOT_CONFIGURED"


class PublicRunError(RuntimeError):
    """Erro público redigido acompanhado do status HTTP apropriado."""

    def __init__(
        self,
        code: PublicRunErrorCode,
        message: str,
        *,
        status_code: int,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class PublicRunRequest(BaseModel):
    """Única entrada controlada pelo navegador; claims confiáveis são campos proibidos."""

    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    message: str = Field(min_length=1, max_length=2000)
    seed: int = Field(default=42, ge=0, le=2_147_483_647)
    context: dict[str, ScopeValue] = Field(default_factory=dict)
    direct_request: bool = False

    @model_validator(mode="after")
    def reject_blank_or_oversized_context(self) -> PublicRunRequest:
        if not self.message.strip():
            raise ValueError("message não pode conter somente espaços")
        # O limite evita transformar pequenos IDs de contexto em um canal para conteúdo livre.
        if sum(len(str(key)) + len(str(value)) for key, value in self.context.items()) > 4096:
            raise ValueError("context excede o limite permitido")
        return self


class PublicConnectorConfig(BaseModel):
    id: str
    name: str
    context_fields: list[str]


class PublicPlaygroundConfig(BaseModel):
    enabled: bool
    model_configured: bool
    execution_mode: str
    connectors: list[PublicConnectorConfig]
    max_message_length: int = 2000
    rate_limit_per_hour: int
    concurrency_limit: int


class PublicRunEvidence(BaseModel):
    id: str
    tool_alias: str
    mcp_tool_name: str
    result: dict[str, Any]
    outcome: str
    status_code: int | None
    truncated: bool


class PublicRunToolCall(BaseModel):
    sequence: int
    tool_alias: str
    mcp_tool_name: str | None
    arguments: dict[str, Any]
    evidence_id: str | None
    status: str
    outcome: str
    latency_ms: float


class PublicRunPolicyDecision(BaseModel):
    tool_sequence: int
    operation_id: str
    outcome: str
    reason_codes: list[str]
    risk: str | None
    required_permission: str | None
    required_scopes: list[str]
    confirmation_required: bool


class PublicRunMetrics(BaseModel):
    model: str
    model_calls: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    latency_ms: float
    termination_reason: str
    truncations: int


class PublicRunResult(BaseModel):
    """Projeção autenticada sem prompt interno, credenciais ou digest de confirmação."""

    run_id: str
    connector_id: str
    status: str
    intent_id: str | None
    decision: str
    answer: str
    evidence_ids: list[str]
    evidence: list[PublicRunEvidence]
    uncertainties: list[str]
    tool_calls: list[PublicRunToolCall]
    policy_decisions: list[PublicRunPolicyDecision]
    metrics: PublicRunMetrics
    observability: dict[str, Any]


class AgentRunRuntime(Protocol):
    """Seam mínimo do host; a implementação concreta é o runtime LangGraph existente."""

    async def run(
        self,
        request: AgentRunRequest,
        trusted_context: TrustedRunContext,
    ) -> AgentRunResult: ...


class PublicRunQuotaDecision(BaseModel):
    allowed: bool
    accepted_runs: int = Field(ge=0)
    reset_at: datetime
    retry_after_seconds: int = Field(ge=0)


class PublicRunQuota(Protocol):
    """Persistência de admissão, separada da execução probabilística."""

    async def consume(self, *, subject: str, limit: int) -> PublicRunQuotaDecision: ...

    async def ready(self) -> bool: ...


class NoOpPublicRunQuota:
    """Placeholder do caminho desligado; nunca autoriza uma execução por acidente."""

    async def consume(self, *, subject: str, limit: int) -> PublicRunQuotaDecision:
        del subject, limit
        raise PublicRunError(
            PublicRunErrorCode.PUBLIC_RUNS_DISABLED,
            "As execuções públicas estão desabilitadas.",
            status_code=503,
        )

    async def ready(self) -> bool:
        return True


class SqlAlchemyPublicRunQuota:
    """Janela de uma hora atômica no PostgreSQL e serializada no processo para SQLite."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> SqlAlchemyPublicRunQuota:
        return cls(create_async_engine(normalize_database_url(url)), now=now)

    async def create_schema_for_tests(self) -> None:
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self._engine.dispose()

    async def ready(self) -> bool:
        async with self._sessions() as session:
            await session.execute(select(PublicRunQuotaRow.subject).limit(1))
        return True

    async def consume(self, *, subject: str, limit: int) -> PublicRunQuotaDecision:
        """Consome somente runs aceitas; chamadas recusadas por concorrência não chegam aqui."""

        now = self._now().astimezone(UTC)
        window = timedelta(hours=1)
        async with self._lock, self._sessions() as session, session.begin():
            statement = (
                select(PublicRunQuotaRow)
                .where(PublicRunQuotaRow.subject == subject)
                .with_for_update()
            )
            row = (await session.execute(statement)).scalar_one_or_none()
            if row is None:
                row = PublicRunQuotaRow(
                    subject=subject,
                    window_started_at=now,
                    accepted_runs=1,
                )
                session.add(row)
                return PublicRunQuotaDecision(
                    allowed=True,
                    accepted_runs=1,
                    reset_at=now + window,
                    retry_after_seconds=0,
                )

            started = _as_utc(row.window_started_at)
            if now >= started + window:
                row.window_started_at = now
                row.accepted_runs = 1
                return PublicRunQuotaDecision(
                    allowed=True,
                    accepted_runs=1,
                    reset_at=now + window,
                    retry_after_seconds=0,
                )

            reset_at = started + window
            if row.accepted_runs >= limit:
                retry_after = max(1, math.ceil((reset_at - now).total_seconds()))
                return PublicRunQuotaDecision(
                    allowed=False,
                    accepted_runs=row.accepted_runs,
                    reset_at=reset_at,
                    retry_after_seconds=retry_after,
                )

            row.accepted_runs += 1
            return PublicRunQuotaDecision(
                allowed=True,
                accepted_runs=row.accepted_runs,
                reset_at=reset_at,
                retry_after_seconds=0,
            )


def _as_utc(value: datetime) -> datetime:
    """SQLite remove o fuso, mas a aplicação persiste sempre timestamps UTC."""

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _public_evidence_result(value: Mapping[str, Any]) -> dict[str, Any]:
    """Remove o digest de confirmação mesmo quando o runtime o devolve dentro da evidência."""

    projected = redact_value(dict(value), redact_strings=True)
    raw_policy = projected.get("policy")
    if isinstance(raw_policy, dict):
        raw_policy.pop("action_digest", None)
    return projected


class PublicRunHost:
    """Esconde todo o caminho público atrás de ``execute`` e uma configuração segura."""

    def __init__(
        self,
        *,
        catalog: ConnectorCatalog | None,
        runtime: AgentRunRuntime | None,
        quota: PublicRunQuota,
        enabled: bool,
        owner_token: str | None,
        public_connector_ids: Sequence[str],
        execution_mode: str = "simulate",
        rate_limit_per_hour: int = 3,
        concurrency_limit: int = 2,
        owner_id: str = "portfolio-owner",
        telemetry: Telemetry | None = None,
    ) -> None:
        self._catalog = catalog
        self._runtime = runtime
        self._quota = quota
        self._enabled = enabled
        self._owner_token = owner_token
        self._public_connector_ids = tuple(public_connector_ids)
        self._execution_mode = execution_mode
        self._rate_limit = rate_limit_per_hour
        self._concurrency_limit = concurrency_limit
        self._owner_id = owner_id
        self._telemetry = telemetry or NoOpTelemetry()
        self._active_runs = 0
        self._admission_lock = asyncio.Lock()

    def config(self) -> PublicPlaygroundConfig:
        connectors: list[PublicConnectorConfig] = []
        if self._catalog is not None:
            by_id = {item.id: item for item in self._catalog.list()}
            for connector_id in self._public_connector_ids:
                connector = by_id.get(connector_id)
                if connector is not None:
                    connectors.append(
                        PublicConnectorConfig(
                            id=connector.id,
                            name=connector.name,
                            context_fields=[
                                field for field in connector.context_fields if field != "user_id"
                            ],
                        )
                    )
        return PublicPlaygroundConfig(
            enabled=self._enabled,
            model_configured=self._runtime is not None,
            execution_mode=self._execution_mode,
            connectors=connectors,
            rate_limit_per_hour=self._rate_limit,
            concurrency_limit=self._concurrency_limit,
        )

    async def ready(self) -> bool:
        if not self._enabled:
            return True
        return (
            self._runtime is not None
            and self._owner_token is not None
            and await self._quota.ready()
        )

    async def execute(
        self,
        authorization: str | None,
        request: PublicRunRequest,
    ) -> PublicRunResult:
        """Autentica, admite, cria claims fixas e executa uma run stateless."""

        if not self._enabled:
            raise PublicRunError(
                PublicRunErrorCode.PUBLIC_RUNS_DISABLED,
                "As execuções públicas estão desabilitadas.",
                status_code=503,
            )
        self._authenticate(authorization)
        if self._runtime is None:
            raise PublicRunError(
                PublicRunErrorCode.MODEL_NOT_CONFIGURED,
                "O modelo do playground ainda não está configurado.",
                status_code=503,
            )
        if request.connector_id not in self._public_connector_ids:
            raise PublicRunError(
                PublicRunErrorCode.CONNECTOR_NOT_PUBLIC,
                "O conector solicitado não está disponível no playground.",
                status_code=403,
            )
        trusted_context = self._trusted_context(request)

        if not await self._try_enter():
            raise PublicRunError(
                PublicRunErrorCode.RUN_CONCURRENCY_LIMIT,
                "O limite de execuções simultâneas foi atingido.",
                status_code=429,
            )
        try:
            quota = await self._quota.consume(subject=self._owner_id, limit=self._rate_limit)
            if not quota.allowed:
                raise PublicRunError(
                    PublicRunErrorCode.RUN_RATE_LIMITED,
                    "O limite de três execuções por hora foi atingido.",
                    status_code=429,
                    retry_after_seconds=quota.retry_after_seconds,
                )
            with self._telemetry.start_span(
                "indusguard.public_run.execute",
                {"indusguard.connector.id": request.connector_id},
            ) as span:
                result = await self._runtime.run(
                    AgentRunRequest(
                        connector_id=request.connector_id,
                        message=request.message,
                        seed=request.seed,
                    ),
                    trusted_context,
                )
                span.set_attribute("indusguard.run.id", result.run_id)
                span.set_attribute("indusguard.run.status", result.status.value)
                return self._project(result)
        finally:
            await self._leave()

    def _authenticate(self, authorization: str | None) -> None:
        if authorization is None:
            raise PublicRunError(
                PublicRunErrorCode.AUTH_REQUIRED,
                "Informe um token Bearer para acessar o playground.",
                status_code=401,
            )
        scheme, separator, candidate = authorization.partition(" ")
        expected = self._owner_token
        valid = (
            separator == " "
            and scheme.lower() == "bearer"
            and bool(candidate)
            and expected is not None
            and secrets.compare_digest(candidate, expected)
        )
        if not valid:
            raise PublicRunError(
                PublicRunErrorCode.AUTH_INVALID,
                "O token Bearer informado é inválido.",
                status_code=401,
            )

    def _trusted_context(self, request: PublicRunRequest) -> TrustedRunContext:
        if self._catalog is None:
            raise PublicRunError(
                PublicRunErrorCode.PUBLIC_RUNS_DISABLED,
                "O catálogo público não está disponível.",
                status_code=503,
            )
        domain = self._catalog.get_domain(request.connector_id)
        if domain is None:
            raise PublicRunError(
                PublicRunErrorCode.CONNECTOR_NOT_PUBLIC,
                "O conector não possui domínio disponível para o agente.",
                status_code=403,
            )
        unexpected = set(request.context) - set(domain.context_fields)
        if unexpected:
            raise PublicRunError(
                PublicRunErrorCode.CONTEXT_INVALID,
                "O contexto contém campos não declarados pelo conector.",
                status_code=422,
            )
        execution_context = dict(request.context)
        # Mesmo se o cliente enviar ``user_id``, a autoridade continua no servidor.
        if "user_id" in domain.context_fields:
            execution_context["user_id"] = self._owner_id
        resource_scopes = {
            key: value for key, value in execution_context.items() if key != "user_id"
        }
        return TrustedRunContext(
            principal=PolicyPrincipal(
                id=self._owner_id,
                permissions=["read", "action_low", "action_high", "escalate"],
                scopes=resource_scopes,
            ),
            execution_context=execution_context,
            resource_scopes=resource_scopes,
            direct_request=request.direct_request,
            confirmation=None,
        )

    async def _try_enter(self) -> bool:
        async with self._admission_lock:
            if self._active_runs >= self._concurrency_limit:
                return False
            self._active_runs += 1
            return True

    async def _leave(self) -> None:
        async with self._admission_lock:
            self._active_runs = max(0, self._active_runs - 1)

    @staticmethod
    def _project(result: AgentRunResult) -> PublicRunResult:
        policies: list[PublicRunPolicyDecision] = []
        evidence_sequence = {
            call.evidence_id: sequence
            for sequence, call in enumerate(result.tool_calls, start=1)
            if call.evidence_id is not None
        }
        for evidence in result.evidence:
            raw_policy = evidence.result.get("policy")
            sequence = evidence_sequence.get(evidence.id)
            if not isinstance(raw_policy, Mapping) or sequence is None:
                continue
            policies.append(
                PublicRunPolicyDecision(
                    tool_sequence=sequence,
                    operation_id=str(raw_policy.get("operation_id", "unknown")),
                    outcome=str(raw_policy.get("outcome", "unknown")),
                    reason_codes=[str(code) for code in raw_policy.get("reason_codes", [])],
                    risk=(str(raw_policy["risk"]) if raw_policy.get("risk") is not None else None),
                    required_permission=(
                        str(raw_policy["required_permission"])
                        if raw_policy.get("required_permission") is not None
                        else None
                    ),
                    required_scopes=[str(scope) for scope in raw_policy.get("required_scopes", [])],
                    confirmation_required=bool(
                        raw_policy.get("confirmation_required_for_execute", False)
                    ),
                )
            )
        return PublicRunResult(
            run_id=result.run_id,
            connector_id=result.connector_id,
            status=result.status.value,
            intent_id=result.intent.intent_id,
            decision=result.decision.value,
            answer=redact_text(result.answer),
            evidence_ids=result.evidence_ids,
            evidence=[
                PublicRunEvidence(
                    id=item.id,
                    tool_alias=item.tool_alias,
                    mcp_tool_name=item.mcp_tool_name,
                    result=_public_evidence_result(item.result),
                    outcome=item.outcome,
                    status_code=item.status_code,
                    truncated=item.truncated,
                )
                for item in result.evidence
            ],
            uncertainties=[redact_text(item) for item in result.uncertainties],
            tool_calls=[
                PublicRunToolCall(
                    sequence=sequence,
                    tool_alias=item.tool_alias,
                    mcp_tool_name=item.mcp_tool_name,
                    arguments=redact_value(item.arguments, redact_strings=True),
                    evidence_id=item.evidence_id,
                    status=item.status,
                    outcome=item.outcome,
                    latency_ms=item.latency_ms,
                )
                for sequence, item in enumerate(result.tool_calls, start=1)
            ],
            policy_decisions=policies,
            metrics=PublicRunMetrics(
                model=result.metrics.model,
                model_calls=result.metrics.model_calls,
                tool_calls=result.metrics.tool_calls,
                input_tokens=result.metrics.input_tokens,
                output_tokens=result.metrics.output_tokens,
                total_tokens=result.metrics.total_tokens,
                latency_ms=result.metrics.latency_ms,
                termination_reason=result.metrics.termination_reason.value,
                truncations=result.metrics.truncations,
            ),
            observability=result.observability.model_dump(mode="json"),
        )
