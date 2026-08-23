"""Persistência incremental do benchmark sobre as tabelas compartilhadas do backend."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from indusguard_api.agent import AgentTerminationReason
from indusguard_api.persistence import (
    EvaluationResultRow,
    EvaluationRunRow,
    SqlAlchemyAgentRunRecorder,
)
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from indusguard_evals.contracts import (
    CaseScore,
    EvaluationPhase,
    EvaluationSample,
    EvaluationVariant,
)

EvaluationStatus = Literal["running", "partial", "completed"]


class PersistedEvaluationRun(BaseModel):
    """Visão imutável usada pelo CLI sem vazar objetos SQLAlchemy."""

    model_config = ConfigDict(extra="forbid")

    evaluation_id: str
    phase: EvaluationPhase
    status: EvaluationStatus
    dataset_version: str
    input_digest: str
    golden_digest: str | None
    model: str
    git_commit: str
    config: dict[str, Any]
    summary: dict[str, Any] | None


class EvaluationRepository:
    """Checkpoint idempotente depois de cada run, compatível com SQLite e PostgreSQL."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def start(
        self,
        *,
        phase: EvaluationPhase,
        dataset_version: str,
        input_digest: str,
        model: str,
        git_commit: str,
        config: dict[str, Any],
    ) -> str:
        evaluation_id = str(uuid4())
        async with self._sessions() as session, session.begin():
            session.add(
                EvaluationRunRow(
                    evaluation_id=evaluation_id,
                    phase=phase.value,
                    status="running",
                    dataset_version=dataset_version,
                    input_digest=input_digest,
                    model=model,
                    git_commit=git_commit,
                    config=config,
                )
            )
        return evaluation_id

    async def checkpoint(
        self,
        evaluation_id: str,
        sample: EvaluationSample,
    ) -> None:
        """Salva observações sem carregar ou persistir qualquer resposta do golden set."""

        scheduled = sample.scheduled
        statement = select(EvaluationResultRow).where(
            EvaluationResultRow.evaluation_id == evaluation_id,
            EvaluationResultRow.case_id == scheduled.case_id,
            EvaluationResultRow.variant == scheduled.variant.value,
            EvaluationResultRow.seed == scheduled.seed,
        )
        async with self._sessions() as session, session.begin():
            existing = (await session.execute(statement)).scalar_one_or_none()
            retryable = (
                sample.result.metrics.termination_reason
                is AgentTerminationReason.MODEL_RATE_LIMITED
            )
            values = {
                "scenario_id": scheduled.scenario_id,
                "ordinal": scheduled.ordinal,
                "result_status": "retryable" if retryable else "completed",
                "termination_reason": sample.result.metrics.termination_reason.value,
                "agent_run_id": sample.result.run_id,
                "observations": {
                    "shadow_policy": [item.model_dump(mode="json") for item in sample.shadow_policy]
                },
                "warnings": [],
            }
            if existing is not None and existing.result_status == "completed":
                return
            if existing is not None:
                for key, value in values.items():
                    setattr(existing, key, value)
                existing.score = None
                return
            session.add(
                EvaluationResultRow(
                    evaluation_id=evaluation_id,
                    case_id=scheduled.case_id,
                    variant=scheduled.variant.value,
                    seed=scheduled.seed,
                    score=None,
                    **values,
                )
            )

    async def completed_identities(
        self, evaluation_id: str
    ) -> set[tuple[str, EvaluationVariant, int]]:
        statement = select(
            EvaluationResultRow.case_id,
            EvaluationResultRow.variant,
            EvaluationResultRow.seed,
        ).where(
            EvaluationResultRow.evaluation_id == evaluation_id,
            EvaluationResultRow.result_status == "completed",
        )
        async with self._sessions() as session:
            rows = (await session.execute(statement)).all()
        return {(case_id, EvaluationVariant(variant), seed) for case_id, variant, seed in rows}

    async def results(self, evaluation_id: str) -> list[CaseScore]:
        statement = (
            select(EvaluationResultRow.score)
            .where(EvaluationResultRow.evaluation_id == evaluation_id)
            .order_by(EvaluationResultRow.ordinal)
        )
        async with self._sessions() as session:
            values = (await session.execute(statement)).scalars().all()
        return [CaseScore.model_validate(value) for value in values if value is not None]

    async def samples(self, evaluation_id: str) -> list[EvaluationSample]:
        """Reconstrói traces seguros; é chamado antes de o runner abrir o golden set."""

        statement = (
            select(EvaluationResultRow)
            .where(
                EvaluationResultRow.evaluation_id == evaluation_id,
                EvaluationResultRow.result_status == "completed",
            )
            .order_by(EvaluationResultRow.ordinal)
        )
        async with self._sessions() as session:
            rows = list((await session.execute(statement)).scalars())
        recorder = SqlAlchemyAgentRunRecorder(self._engine)
        samples: list[EvaluationSample] = []
        for row in rows:
            persisted = await recorder.get(row.agent_run_id)
            if persisted is None:
                raise RuntimeError(f"agent_run ausente para checkpoint {row.id}")
            samples.append(
                EvaluationSample(
                    scheduled={
                        "case_id": row.case_id,
                        "scenario_id": row.scenario_id,
                        "variant": row.variant,
                        "seed": row.seed,
                        "ordinal": row.ordinal,
                    },
                    result=persisted.result,
                    shadow_policy=row.observations.get("shadow_policy", []),
                )
            )
        return samples

    async def apply_score(
        self,
        evaluation_id: str,
        score: CaseScore,
    ) -> None:
        statement = select(EvaluationResultRow).where(
            EvaluationResultRow.evaluation_id == evaluation_id,
            EvaluationResultRow.case_id == score.case_id,
            EvaluationResultRow.variant == score.variant.value,
            EvaluationResultRow.seed == score.seed,
        )
        async with self._sessions() as session, session.begin():
            row = (await session.execute(statement)).scalar_one_or_none()
            if row is None:
                raise KeyError("checkpoint não encontrado para score")
            row.score = score.model_dump(mode="json")
            row.warnings = score.warnings

    async def finish(
        self,
        evaluation_id: str,
        *,
        status: EvaluationStatus,
        summary: dict[str, Any],
        golden_digest: str | None = None,
    ) -> None:
        async with self._sessions() as session, session.begin():
            row = await session.get(EvaluationRunRow, evaluation_id)
            if row is None:
                raise KeyError(f"avaliação não encontrada: {evaluation_id}")
            row.status = status
            row.summary = summary
            row.golden_digest = golden_digest
            row.completed_at = datetime.now(UTC)

    async def get(self, evaluation_id: str) -> PersistedEvaluationRun | None:
        async with self._sessions() as session:
            row = await session.get(EvaluationRunRow, evaluation_id)
            if row is None:
                return None
            return PersistedEvaluationRun(
                evaluation_id=row.evaluation_id,
                phase=EvaluationPhase(row.phase),
                status=row.status,
                dataset_version=row.dataset_version,
                input_digest=row.input_digest,
                golden_digest=row.golden_digest,
                model=row.model,
                git_commit=row.git_commit,
                config=row.config,
                summary=row.summary,
            )
