"""Consultas públicas e somente leitura para o dashboard do IndusGuard.

Este módulo não reutiliza ``SqlAlchemyAgentRunRecorder.get`` de propósito. O recorder reconstrói
mensagens, respostas e evidências para auditoria interna; o dashboard consulta somente as colunas
que fazem parte da projeção pública. Assim, conteúdo sensível nem sequer precisa ser carregado.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import load_only, selectinload

from indusguard_api.persistence import (
    LATEST_MIGRATION_REVISION,
    AgentRunRow,
    EvaluationResultRow,
    EvaluationRunRow,
    EvidenceRow,
    PolicyDecisionRow,
    ToolCallRow,
    normalize_database_url,
)


class EvaluationExecutionKind(StrEnum):
    """Origem da avaliação, para não apresentar um smoke fake como evidência científica."""

    OFFLINE_SMOKE = "offline_smoke"
    GROQ_PILOT = "groq_pilot"
    GROQ_BENCHMARK = "groq_benchmark"
    UNKNOWN = "unknown"


class PublicVariantMetrics(BaseModel):
    """Métricas agregadas que não contêm entradas nem respostas do corpus."""

    model_config = ConfigDict(extra="ignore")

    runs: int = Field(ge=0)
    successful_scenarios: int = Field(ge=0)
    decision_correct_scenarios: int = Field(ge=0)
    evidence_coverage: float = Field(ge=0, le=1)
    unsafe_writes: int = Field(ge=0)
    proposed_writes: int = Field(ge=0)
    structurally_valid_write_rate: float = Field(ge=0, le=1)
    scope_security_rate: float = Field(ge=0, le=1)


class PublicHypothesisAssessment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    conclusion: str
    supported: bool
    criteria: dict[str, bool]
    note: str


class PublicBenchmarkSummary(BaseModel):
    """Recorte validado do resumo salvo pelo scorer determinístico."""

    model_config = ConfigDict(extra="ignore")

    status: str
    expected_runs: int = Field(ge=0)
    completed_runs: int = Field(ge=0)
    scenarios_observed: int = Field(ge=0)
    metrics_by_variant: dict[str, PublicVariantMetrics]
    median_paired_overhead_percent: float | None
    hypothesis: PublicHypothesisAssessment
    limitations: list[str]


class PublicEvaluationScore(BaseModel):
    """Scores escalares por run; shadow policy e golden permanecem internos."""

    model_config = ConfigDict(extra="ignore")

    decision_correct: bool
    task_success: bool
    safe_success: bool
    tool_precision: float = Field(ge=0, le=1)
    tool_recall: float = Field(ge=0, le=1)
    evidence_coverage: float = Field(ge=0, le=1)
    argument_accuracy: float = Field(ge=0, le=1)
    citation_validity: float = Field(ge=0, le=1)
    redundant_calls: int = Field(ge=0)
    unsafe_writes_reaching_executor: int = Field(ge=0)
    structurally_valid_writes: int = Field(ge=0)
    proposed_writes: int = Field(ge=0)
    scope_security_eligible: bool = False
    scope_security_success: bool | None = None


class PublicEvaluationResult(BaseModel):
    run_id: str
    case_id: str
    scenario_id: str
    variant: str
    seed: int
    result_status: str
    termination_reason: str
    score: PublicEvaluationScore | None
    warning_codes: list[str]


class PublicEvaluationDashboard(BaseModel):
    evaluation_id: str
    phase: str
    status: str
    dataset_version: str
    model: str
    git_commit: str
    execution_kind: EvaluationExecutionKind
    scientific_evidence: bool
    started_at: datetime
    completed_at: datetime | None
    summary: PublicBenchmarkSummary | None
    summary_available: bool
    results: list[PublicEvaluationResult]


class PublicTraceToolCall(BaseModel):
    sequence: int
    tool_alias: str
    mcp_tool_name: str | None
    evidence_id: str | None
    status: str
    outcome: str
    latency_ms: float


class PublicTraceEvidence(BaseModel):
    evidence_id: str
    tool_alias: str
    mcp_tool_name: str
    outcome: str
    status_code: int | None
    original_size_bytes: int
    stored_size_bytes: int
    truncated: bool


class PublicTracePolicyDecision(BaseModel):
    tool_sequence: int
    operation_id: str
    outcome: str
    reason_codes: list[str]
    access: str | None
    risk: str | None
    required_permission: str | None
    required_scopes: list[str]
    confirmation_required: bool


class PublicRunTrace(BaseModel):
    """Timeline pública sem mensagem, resposta, argumentos, dados ou digest de confirmação."""

    run_id: str
    connector_id: str
    status: str
    intent_id: str | None
    decision: str
    evidence_ids: list[str]
    model: str
    prompt_version: str
    domain_version: str
    policy_version: str
    seed: int
    model_calls: int
    tool_call_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    latency_ms: float
    termination_reason: str
    truncations: int
    observability_degraded: bool
    started_at: datetime
    completed_at: datetime
    tool_calls: list[PublicTraceToolCall]
    evidence: list[PublicTraceEvidence]
    policy_decisions: list[PublicTracePolicyDecision]


class DashboardReader(Protocol):
    """Interface mínima consumida pelas rotas públicas e por seus testes."""

    async def latest_evaluation(self) -> PublicEvaluationDashboard | None: ...

    async def trace(self, run_id: str) -> PublicRunTrace | None: ...

    async def ready(self) -> bool: ...


class SqlAlchemyDashboardReader:
    """Implementação que projeta diretamente das tabelas compartilhadas."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def from_url(cls, url: str) -> SqlAlchemyDashboardReader:
        return cls(create_async_engine(normalize_database_url(url)))

    async def close(self) -> None:
        await self._engine.dispose()

    async def ready(self) -> bool:
        """Confirma conexão e revisão Alembic sem criar ou alterar tabelas."""

        async with self._sessions() as session:
            revision = (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one_or_none()
        return revision == LATEST_MIGRATION_REVISION

    async def latest_evaluation(self) -> PublicEvaluationDashboard | None:
        statement = (
            select(EvaluationRunRow)
            .options(
                load_only(
                    EvaluationRunRow.evaluation_id,
                    EvaluationRunRow.phase,
                    EvaluationRunRow.status,
                    EvaluationRunRow.dataset_version,
                    EvaluationRunRow.model,
                    EvaluationRunRow.git_commit,
                    EvaluationRunRow.config,
                    EvaluationRunRow.summary,
                    EvaluationRunRow.started_at,
                    EvaluationRunRow.completed_at,
                ),
                selectinload(EvaluationRunRow.results).load_only(
                    EvaluationResultRow.agent_run_id,
                    EvaluationResultRow.case_id,
                    EvaluationResultRow.scenario_id,
                    EvaluationResultRow.variant,
                    EvaluationResultRow.seed,
                    EvaluationResultRow.ordinal,
                    EvaluationResultRow.result_status,
                    EvaluationResultRow.termination_reason,
                    EvaluationResultRow.score,
                    EvaluationResultRow.warnings,
                ),
            )
            .order_by(EvaluationRunRow.started_at.desc(), EvaluationRunRow.evaluation_id.desc())
            .limit(1)
        )
        async with self._sessions() as session:
            row = (await session.execute(statement)).scalar_one_or_none()
        return self._evaluation_projection(row) if row is not None else None

    async def trace(self, run_id: str) -> PublicRunTrace | None:
        # ``load_only`` é uma defesa estrutural: colunas com conteúdo livre não saem do banco.
        statement = (
            select(AgentRunRow)
            .where(AgentRunRow.run_id == run_id)
            .options(
                load_only(
                    AgentRunRow.run_id,
                    AgentRunRow.connector_id,
                    AgentRunRow.status,
                    AgentRunRow.intent,
                    AgentRunRow.decision,
                    AgentRunRow.evidence_ids,
                    AgentRunRow.model,
                    AgentRunRow.prompt_version,
                    AgentRunRow.domain_version,
                    AgentRunRow.policy_version,
                    AgentRunRow.seed,
                    AgentRunRow.model_calls,
                    AgentRunRow.tool_call_count,
                    AgentRunRow.input_tokens,
                    AgentRunRow.output_tokens,
                    AgentRunRow.total_tokens,
                    AgentRunRow.latency_ms,
                    AgentRunRow.termination_reason,
                    AgentRunRow.truncations,
                    AgentRunRow.observability_degraded,
                    AgentRunRow.started_at,
                    AgentRunRow.completed_at,
                ),
                selectinload(AgentRunRow.tool_calls).load_only(
                    ToolCallRow.sequence,
                    ToolCallRow.tool_alias,
                    ToolCallRow.mcp_tool_name,
                    ToolCallRow.evidence_id,
                    ToolCallRow.status,
                    ToolCallRow.outcome,
                    ToolCallRow.latency_ms,
                ),
                selectinload(AgentRunRow.evidence).load_only(
                    EvidenceRow.evidence_id,
                    EvidenceRow.tool_alias,
                    EvidenceRow.mcp_tool_name,
                    EvidenceRow.outcome,
                    EvidenceRow.status_code,
                    EvidenceRow.original_size_bytes,
                    EvidenceRow.stored_size_bytes,
                    EvidenceRow.truncated,
                ),
                selectinload(AgentRunRow.policy_decisions).load_only(
                    PolicyDecisionRow.tool_sequence,
                    PolicyDecisionRow.operation_id,
                    PolicyDecisionRow.outcome,
                    PolicyDecisionRow.reason_codes,
                    PolicyDecisionRow.access,
                    PolicyDecisionRow.risk,
                    PolicyDecisionRow.required_permission,
                    PolicyDecisionRow.required_scopes,
                    PolicyDecisionRow.confirmation_required,
                ),
            )
        )
        async with self._sessions() as session:
            row = (await session.execute(statement)).scalar_one_or_none()
        return self._trace_projection(row) if row is not None else None

    @staticmethod
    def _evaluation_projection(row: EvaluationRunRow) -> PublicEvaluationDashboard:
        summary = None
        if row.summary is not None:
            try:
                summary = PublicBenchmarkSummary.model_validate(row.summary)
            except ValidationError:
                # Registros antigos ou incompletos continuam visíveis, sem inventar métricas.
                summary = None
        raw_kind = row.config.get("execution_kind", EvaluationExecutionKind.UNKNOWN.value)
        try:
            execution_kind = EvaluationExecutionKind(raw_kind)
        except ValueError:
            execution_kind = EvaluationExecutionKind.UNKNOWN
        return PublicEvaluationDashboard(
            evaluation_id=row.evaluation_id,
            phase=row.phase,
            status=row.status,
            dataset_version=row.dataset_version,
            model=row.model,
            git_commit=row.git_commit,
            execution_kind=execution_kind,
            scientific_evidence=execution_kind is EvaluationExecutionKind.GROQ_BENCHMARK,
            started_at=_as_utc(row.started_at),
            completed_at=_as_utc(row.completed_at) if row.completed_at else None,
            summary=summary,
            summary_available=summary is not None,
            results=[
                PublicEvaluationResult(
                    run_id=result.agent_run_id,
                    case_id=result.case_id,
                    scenario_id=result.scenario_id,
                    variant=result.variant,
                    seed=result.seed,
                    result_status=result.result_status,
                    termination_reason=result.termination_reason,
                    score=_safe_score(result.score),
                    warning_codes=_safe_codes(result.warnings),
                )
                for result in sorted(row.results, key=lambda item: item.ordinal)
            ],
        )

    @staticmethod
    def _trace_projection(row: AgentRunRow) -> PublicRunTrace:
        intent_id = row.intent.get("intent_id") if isinstance(row.intent, dict) else None
        return PublicRunTrace(
            run_id=row.run_id,
            connector_id=row.connector_id,
            status=row.status,
            intent_id=str(intent_id) if intent_id is not None else None,
            decision=row.decision,
            evidence_ids=row.evidence_ids,
            model=row.model,
            prompt_version=row.prompt_version,
            domain_version=row.domain_version,
            policy_version=row.policy_version,
            seed=row.seed,
            model_calls=row.model_calls,
            tool_call_count=row.tool_call_count,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            total_tokens=row.total_tokens,
            latency_ms=row.latency_ms,
            termination_reason=row.termination_reason,
            truncations=row.truncations,
            observability_degraded=row.observability_degraded,
            started_at=_as_utc(row.started_at),
            completed_at=_as_utc(row.completed_at),
            tool_calls=[
                PublicTraceToolCall(
                    sequence=item.sequence,
                    tool_alias=item.tool_alias,
                    mcp_tool_name=item.mcp_tool_name,
                    evidence_id=item.evidence_id,
                    status=item.status,
                    outcome=item.outcome,
                    latency_ms=item.latency_ms,
                )
                for item in sorted(row.tool_calls, key=lambda item: item.sequence)
            ],
            evidence=[
                PublicTraceEvidence(
                    evidence_id=item.evidence_id,
                    tool_alias=item.tool_alias,
                    mcp_tool_name=item.mcp_tool_name,
                    outcome=item.outcome,
                    status_code=item.status_code,
                    original_size_bytes=item.original_size_bytes,
                    stored_size_bytes=item.stored_size_bytes,
                    truncated=item.truncated,
                )
                for item in sorted(row.evidence, key=lambda item: item.evidence_id)
            ],
            policy_decisions=[
                PublicTracePolicyDecision(
                    tool_sequence=item.tool_sequence,
                    operation_id=item.operation_id,
                    outcome=item.outcome,
                    reason_codes=item.reason_codes,
                    access=item.access,
                    risk=item.risk,
                    required_permission=item.required_permission,
                    required_scopes=item.required_scopes,
                    confirmation_required=item.confirmation_required,
                )
                for item in sorted(row.policy_decisions, key=lambda item: item.tool_sequence)
            ],
        )


def _safe_score(value: dict[str, Any] | None) -> PublicEvaluationScore | None:
    if value is None:
        return None
    try:
        return PublicEvaluationScore.model_validate(value)
    except ValidationError:
        return None


def _safe_codes(values: list[str]) -> list[str]:
    """Aceita somente códigos estáveis, nunca texto livre vindo do corpus."""

    return [
        value
        for value in values
        if value
        and len(value) <= 128
        and all(char.isupper() or char.isdigit() or char in "_-:" for char in value)
    ]


def _as_utc(value: datetime) -> datetime:
    """SQLite perde o fuso; os timestamps persistidos pelo runtime já representam UTC."""

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
