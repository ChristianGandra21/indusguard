"""Persistência transacional das runs redigidas do agente.

SQLAlchemy isola o domínio do banco concreto. SQLite é suficiente para estudo e testes locais;
uma URL PostgreSQL usa o mesmo recorder no Neon. O módulo nunca recebe ``TrustedRunContext`` e,
portanto, não consegue persistir credenciais, permissões ou identidade do principal.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, selectinload

from indusguard_api.agent import (
    AgentDecision,
    AgentEvidence,
    AgentIntentDecision,
    AgentObservability,
    AgentRunMetrics,
    AgentRunRequest,
    AgentRunResult,
    AgentRunStatus,
    AgentTerminationReason,
    AgentToolCall,
)
from indusguard_api.redaction import redact_text, redact_value

LATEST_MIGRATION_REVISION = "20260824_0003"


class Base(DeclarativeBase):
    """Metadata única usada pelo runtime, testes e Alembic."""


class AgentRunRow(Base):
    __tablename__ = "agent_runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    connector_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    intent: Mapped[dict[str, Any]] = mapped_column(JSON)
    decision: Mapped[str] = mapped_column(String(32), index=True)
    request_message: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON)
    uncertainties: Mapped[list[str]] = mapped_column(JSON)
    observability: Mapped[dict[str, Any]] = mapped_column(JSON)
    model: Mapped[str] = mapped_column(String(255))
    prompt_version: Mapped[str] = mapped_column(String(128))
    domain_version: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(String(128))
    seed: Mapped[int] = mapped_column(Integer)
    model_calls: Mapped[int] = mapped_column(Integer)
    tool_call_count: Mapped[int] = mapped_column(Integer)
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    total_tokens: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[float] = mapped_column(Float)
    termination_reason: Mapped[str] = mapped_column(String(64), index=True)
    truncations: Mapped[int] = mapped_column(Integer)
    observability_degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[Any] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Any] = mapped_column(DateTime(timezone=True))

    tool_calls: Mapped[list[ToolCallRow]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    evidence: Mapped[list[EvidenceRow]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    policy_decisions: Mapped[list[PolicyDecisionRow]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ToolCallRow(Base):
    __tablename__ = "tool_calls"
    __table_args__ = (Index("ix_tool_calls_run_sequence", "run_id", "sequence", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.run_id", ondelete="CASCADE"),
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer)
    tool_alias: Mapped[str] = mapped_column(String(128))
    mcp_tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON)
    evidence_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    outcome: Mapped[str] = mapped_column(String(128), index=True)
    latency_ms: Mapped[float] = mapped_column(Float)

    run: Mapped[AgentRunRow] = relationship(back_populates="tool_calls")


class EvidenceRow(Base):
    __tablename__ = "agent_evidence"
    __table_args__ = (
        Index("ix_agent_evidence_run_evidence", "run_id", "evidence_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.run_id", ondelete="CASCADE"),
        index=True,
    )
    evidence_id: Mapped[str] = mapped_column(String(32))
    tool_alias: Mapped[str] = mapped_column(String(128))
    mcp_tool_name: Mapped[str] = mapped_column(String(128))
    result: Mapped[dict[str, Any]] = mapped_column(JSON)
    outcome: Mapped[str] = mapped_column(String(128), index=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    original_size_bytes: Mapped[int] = mapped_column(Integer)
    stored_size_bytes: Mapped[int] = mapped_column(Integer)
    truncated: Mapped[bool] = mapped_column(Boolean)

    run: Mapped[AgentRunRow] = relationship(back_populates="evidence")


class PolicyDecisionRow(Base):
    __tablename__ = "policy_decisions"
    __table_args__ = (
        Index("ix_policy_decisions_run_sequence", "run_id", "tool_sequence", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.run_id", ondelete="CASCADE"),
        index=True,
    )
    tool_sequence: Mapped[int] = mapped_column(Integer)
    operation_id: Mapped[str] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(String(64), index=True)
    reason_codes: Mapped[list[str]] = mapped_column(JSON)
    access: Mapped[str | None] = mapped_column(String(32), nullable=True)
    risk: Mapped[str | None] = mapped_column(String(32), nullable=True)
    required_permission: Mapped[str | None] = mapped_column(String(128), nullable=True)
    required_scopes: Mapped[list[str]] = mapped_column(JSON)
    confirmation_required: Mapped[bool] = mapped_column(Boolean)
    action_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)

    run: Mapped[AgentRunRow] = relationship(back_populates="policy_decisions")


class PublicRunQuotaRow(Base):
    """Janela persistente do proprietário; não armazena token, IP ou conteúdo da run."""

    __tablename__ = "public_run_quota"

    subject: Mapped[str] = mapped_column(String(128), primary_key=True)
    window_started_at: Mapped[Any] = mapped_column(DateTime(timezone=True))
    accepted_runs: Mapped[int] = mapped_column(Integer)


class EvaluationRunRow(Base):
    """Metadados reproduzíveis do benchmark, sem armazenar o conteúdo do golden set."""

    __tablename__ = "evaluation_runs"

    evaluation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    phase: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    dataset_version: Mapped[str] = mapped_column(String(128), index=True)
    input_digest: Mapped[str] = mapped_column(String(64))
    golden_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str] = mapped_column(String(255))
    git_commit: Mapped[str] = mapped_column(String(64))
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    completed_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)

    results: Mapped[list[EvaluationResultRow]] = relationship(
        back_populates="evaluation",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class EvaluationResultRow(Base):
    """Checkpoint de uma identidade case × variante × seed ligado à run do agente."""

    __tablename__ = "evaluation_results"
    __table_args__ = (
        UniqueConstraint(
            "evaluation_id",
            "case_id",
            "variant",
            "seed",
            name="uq_evaluation_result_identity",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    evaluation_id: Mapped[str] = mapped_column(
        ForeignKey("evaluation_runs.evaluation_id", ondelete="CASCADE"), index=True
    )
    case_id: Mapped[str] = mapped_column(String(128), index=True)
    scenario_id: Mapped[str] = mapped_column(String(32), index=True)
    variant: Mapped[str] = mapped_column(String(32), index=True)
    seed: Mapped[int] = mapped_column(Integer)
    ordinal: Mapped[int] = mapped_column(Integer)
    result_status: Mapped[str] = mapped_column(String(32), index=True)
    termination_reason: Mapped[str] = mapped_column(String(64), index=True)
    agent_run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.run_id", ondelete="RESTRICT"), index=True
    )
    observations: Mapped[dict[str, Any]] = mapped_column(JSON)
    score: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    warnings: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    evaluation: Mapped[EvaluationRunRow] = relationship(back_populates="results")


class PersistedAgentRun(BaseModel):
    """Contrato de leitura usado por testes e pela futura rota de trace."""

    model_config = ConfigDict(extra="forbid")

    request_message: str
    result: AgentRunResult
    policy_decisions: list[dict[str, Any]]


def normalize_database_url(url: str) -> str:
    """Adiciona o driver async quando o Neon fornece uma URL PostgreSQL convencional."""

    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


class SqlAlchemyAgentRunRecorder:
    """Implementação atômica e idempotente do recorder do agente."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def from_url(cls, url: str, *, echo: bool = False) -> SqlAlchemyAgentRunRecorder:
        return cls(create_async_engine(normalize_database_url(url), echo=echo))

    async def create_schema_for_tests(self) -> None:
        """Cria tabelas somente em testes; deployments devem executar Alembic."""

        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self._engine.dispose()

    async def record(self, *, request: AgentRunRequest, result: AgentRunResult) -> None:
        """Salva pai e filhos em uma única transação; repetição do mesmo run_id é no-op."""

        async with self._sessions() as session, session.begin():
            if await session.get(AgentRunRow, result.run_id) is not None:
                return
            row = self._to_row(request, result)
            session.add(row)

    async def get(self, run_id: str) -> PersistedAgentRun | None:
        """Reconstrói o resultado seguro sem devolver objetos SQLAlchemy ao chamador."""

        statement = (
            select(AgentRunRow)
            .where(AgentRunRow.run_id == run_id)
            .options(
                selectinload(AgentRunRow.tool_calls),
                selectinload(AgentRunRow.evidence),
                selectinload(AgentRunRow.policy_decisions),
            )
        )
        async with self._sessions() as session:
            row = (await session.execute(statement)).scalar_one_or_none()
            return self._from_row(row) if row else None

    @staticmethod
    def _to_row(request: AgentRunRequest, result: AgentRunResult) -> AgentRunRow:
        metrics = result.metrics
        row = AgentRunRow(
            run_id=result.run_id,
            connector_id=result.connector_id,
            status=result.status.value,
            intent=result.intent.model_dump(mode="json"),
            decision=result.decision.value,
            request_message=redact_text(request.message),
            answer=redact_text(result.answer),
            evidence_ids=result.evidence_ids,
            uncertainties=result.uncertainties,
            observability=result.observability.model_dump(mode="json"),
            model=metrics.model,
            prompt_version=metrics.prompt_version,
            domain_version=metrics.domain_version,
            policy_version=metrics.policy_version,
            seed=request.seed,
            model_calls=metrics.model_calls,
            tool_call_count=metrics.tool_calls,
            input_tokens=metrics.input_tokens,
            output_tokens=metrics.output_tokens,
            total_tokens=metrics.total_tokens,
            latency_ms=metrics.latency_ms,
            termination_reason=metrics.termination_reason.value,
            truncations=metrics.truncations,
            observability_degraded=metrics.observability_degraded,
            started_at=result.started_at,
            completed_at=result.completed_at,
        )
        row.tool_calls = [
            ToolCallRow(
                sequence=sequence,
                tool_alias=call.tool_alias,
                mcp_tool_name=call.mcp_tool_name,
                arguments=redact_value(call.arguments, redact_strings=True),
                evidence_id=call.evidence_id,
                status=call.status,
                outcome=call.outcome,
                latency_ms=call.latency_ms,
            )
            for sequence, call in enumerate(result.tool_calls, start=1)
        ]
        row.evidence = [
            EvidenceRow(
                evidence_id=evidence.id,
                tool_alias=evidence.tool_alias,
                mcp_tool_name=evidence.mcp_tool_name,
                result=redact_value(evidence.result, redact_strings=True),
                outcome=evidence.outcome,
                status_code=evidence.status_code,
                original_size_bytes=evidence.original_size_bytes,
                stored_size_bytes=evidence.stored_size_bytes,
                truncated=evidence.truncated,
            )
            for evidence in result.evidence
        ]
        row.policy_decisions = _policy_rows(result)
        return row

    @staticmethod
    def _from_row(row: AgentRunRow) -> PersistedAgentRun:
        tool_calls = [
            AgentToolCall(
                tool_alias=call.tool_alias,
                mcp_tool_name=call.mcp_tool_name,
                arguments=call.arguments,
                evidence_id=call.evidence_id,
                status=call.status,
                outcome=call.outcome,
                latency_ms=call.latency_ms,
            )
            for call in sorted(row.tool_calls, key=lambda item: item.sequence)
        ]
        evidence = [
            AgentEvidence(
                id=item.evidence_id,
                tool_alias=item.tool_alias,
                mcp_tool_name=item.mcp_tool_name,
                result=item.result,
                outcome=item.outcome,
                status_code=item.status_code,
                original_size_bytes=item.original_size_bytes,
                stored_size_bytes=item.stored_size_bytes,
                truncated=item.truncated,
            )
            for item in sorted(row.evidence, key=lambda item: item.evidence_id)
        ]
        metrics = AgentRunMetrics(
            model=row.model,
            prompt_version=row.prompt_version,
            domain_version=row.domain_version,
            policy_version=row.policy_version,
            model_calls=row.model_calls,
            tool_calls=row.tool_call_count,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            total_tokens=row.total_tokens,
            latency_ms=row.latency_ms,
            termination_reason=AgentTerminationReason(row.termination_reason),
            truncations=row.truncations,
            observability_degraded=row.observability_degraded,
        )
        result = AgentRunResult(
            run_id=row.run_id,
            started_at=row.started_at,
            completed_at=row.completed_at,
            connector_id=row.connector_id,
            status=AgentRunStatus(row.status),
            intent=AgentIntentDecision.model_validate(row.intent),
            decision=AgentDecision(row.decision),
            answer=row.answer,
            evidence_ids=row.evidence_ids,
            evidence=evidence,
            uncertainties=row.uncertainties,
            tool_calls=tool_calls,
            metrics=metrics,
            observability=AgentObservability.model_validate(row.observability),
        )
        policies = [
            {
                "tool_sequence": policy.tool_sequence,
                "operation_id": policy.operation_id,
                "outcome": policy.outcome,
                "reason_codes": policy.reason_codes,
                "access": policy.access,
                "risk": policy.risk,
                "required_permission": policy.required_permission,
                "required_scopes": policy.required_scopes,
                "confirmation_required": policy.confirmation_required,
                "action_digest": policy.action_digest,
            }
            for policy in sorted(row.policy_decisions, key=lambda item: item.tool_sequence)
        ]
        return PersistedAgentRun(
            request_message=row.request_message,
            result=result,
            policy_decisions=policies,
        )


def _policy_rows(result: AgentRunResult) -> list[PolicyDecisionRow]:
    """Extrai somente o envelope político já redigido das evidências MCP."""

    rows: list[PolicyDecisionRow] = []
    tool_sequence = {
        call.evidence_id: sequence
        for sequence, call in enumerate(result.tool_calls, start=1)
        if call.evidence_id is not None
    }
    for evidence in result.evidence:
        raw_policy = evidence.result.get("policy")
        sequence = tool_sequence.get(evidence.id)
        if not isinstance(raw_policy, Mapping) or sequence is None:
            continue
        rows.append(
            PolicyDecisionRow(
                tool_sequence=sequence,
                operation_id=str(raw_policy.get("operation_id", "unknown")),
                outcome=str(raw_policy.get("outcome", "unknown")),
                reason_codes=[str(code) for code in raw_policy.get("reason_codes", [])],
                access=_optional_string(raw_policy.get("access")),
                risk=_optional_string(raw_policy.get("risk")),
                required_permission=_optional_string(raw_policy.get("required_permission")),
                required_scopes=[str(scope) for scope in raw_policy.get("required_scopes", [])],
                confirmation_required=bool(
                    raw_policy.get("confirmation_required_for_execute", False)
                ),
                action_digest=_optional_string(raw_policy.get("action_digest")),
            )
        )
    return rows


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None
