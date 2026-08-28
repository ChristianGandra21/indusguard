"""Interrupção por cota e retomada sem duplicar identidades experimentais."""

import asyncio
from pathlib import Path
from uuid import uuid4

from indusguard_api.agent import AgentRunRequest, AgentTerminationReason
from indusguard_api.connectors import ConnectorCatalog
from indusguard_api.persistence import Base, SqlAlchemyAgentRunRecorder
from sqlalchemy.ext.asyncio import create_async_engine

from indusguard_evals.contracts import (
    EvaluationPhase,
    EvaluationSample,
    EvaluationVariant,
)
from indusguard_evals.corpus import OfficialCorpus
from indusguard_evals.report import BenchmarkSummary
from indusguard_evals.repository import EvaluationRepository
from indusguard_evals.runner import BenchmarkRunner, EvaluationProgress
from tests.factories import agent_result

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CORPUS_ROOT = REPOSITORY_ROOT / "evals" / "corpus" / "official-v1"


class FakeVariantRuntime:
    def __init__(
        self,
        variant: EvaluationVariant,
        recorder: SqlAlchemyAgentRunRecorder,
        *,
        rate_limit_once: bool = False,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.variant = variant
        self._recorder = recorder
        self._rate_limit_once = rate_limit_once
        self._retry_after_seconds = retry_after_seconds

    async def run(self, scheduled: object, case: object) -> EvaluationSample:
        termination = (
            AgentTerminationReason.MODEL_RATE_LIMITED
            if self._rate_limit_once
            else AgentTerminationReason.COMPLETED
        )
        self._rate_limit_once = False
        result = agent_result(termination=termination).model_copy(update={"run_id": str(uuid4())})
        if termination is AgentTerminationReason.MODEL_RATE_LIMITED:
            result = result.model_copy(
                update={
                    "metrics": result.metrics.model_copy(
                        update={"retry_after_seconds": self._retry_after_seconds}
                    )
                }
            )
        request = AgentRunRequest(
            connector_id="tractian",
            message=case.message,
            seed=scheduled.seed,
        )
        await self._recorder.record(request=request, result=result)
        return EvaluationSample(scheduled=scheduled, result=result)


def test_rate_limit_creates_partial_checkpoint_and_resume_finishes_12_runs(
    tmp_path: Path,
) -> None:
    async def exercise() -> tuple[
        BenchmarkSummary,
        BenchmarkSummary,
        int,
        list[EvaluationProgress],
        list[EvaluationProgress],
        dict[str, object] | None,
    ]:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runner.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        repository = EvaluationRepository(engine)
        recorder = SqlAlchemyAgentRunRecorder(engine)
        catalog = ConnectorCatalog(REPOSITORY_ROOT / "connectors")
        catalog.load()
        corpus = OfficialCorpus(CORPUS_ROOT)
        partial_progress: list[EvaluationProgress] = []
        first_runner = BenchmarkRunner(
            corpus=corpus,
            catalog=catalog,
            repository=repository,
            runtimes={
                EvaluationVariant.PROMPT_ONLY: FakeVariantRuntime(
                    EvaluationVariant.PROMPT_ONLY,
                    recorder,
                    rate_limit_once=True,
                    retry_after_seconds=90,
                ),
                EvaluationVariant.GUARDED: FakeVariantRuntime(
                    EvaluationVariant.GUARDED,
                    recorder,
                ),
            },
            on_progress=partial_progress.append,
        )
        evaluation_id = await first_runner.start(
            phase=EvaluationPhase.PILOT,
            model="fake-eval-model",
            git_commit="abc123",
        )
        partial = await first_runner.execute(evaluation_id)
        persisted_partial = await repository.get(evaluation_id)
        resumed_progress: list[EvaluationProgress] = []
        resumed_runner = BenchmarkRunner(
            corpus=corpus,
            catalog=catalog,
            repository=repository,
            runtimes={
                variant: FakeVariantRuntime(variant, recorder) for variant in EvaluationVariant
            },
            on_progress=resumed_progress.append,
        )
        completed = await resumed_runner.execute(evaluation_id)
        identities = await repository.completed_identities(evaluation_id)
        await engine.dispose()
        return (
            partial,
            completed,
            len(identities),
            partial_progress,
            resumed_progress,
            persisted_partial.summary if persisted_partial else None,
        )

    partial, completed, identity_count, partial_progress, resumed_progress, persisted_summary = (
        asyncio.run(exercise())
    )

    assert partial.status == "partial"
    assert partial.completed_runs == 0
    assert partial.interruption is not None
    assert partial.interruption.code == "MODEL_RATE_LIMITED"
    assert partial.interruption.retry_after_seconds == 90
    assert partial.interruption.resume_not_before is not None
    assert persisted_summary is not None
    assert persisted_summary["interruption"]["code"] == "MODEL_RATE_LIMITED"
    assert partial_progress[0].checkpoint_status == "rate_limited"
    assert partial_progress[0].completed_runs == 0
    assert completed.status == "completed"
    assert completed.completed_runs == 12
    assert completed.interruption is None
    assert len(resumed_progress) == 12
    assert resumed_progress[-1].completed_runs == 12
    assert all(item.checkpoint_status == "completed" for item in resumed_progress)
    assert identity_count == 12


def test_evaluation_persists_the_authorized_preflight_digest(tmp_path: Path) -> None:
    async def exercise() -> str | None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'preflight.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        repository = EvaluationRepository(engine)
        recorder = SqlAlchemyAgentRunRecorder(engine)
        catalog = ConnectorCatalog(REPOSITORY_ROOT / "connectors")
        catalog.load()
        runner = BenchmarkRunner(
            corpus=OfficialCorpus(CORPUS_ROOT),
            catalog=catalog,
            repository=repository,
            runtimes={
                variant: FakeVariantRuntime(variant, recorder) for variant in EvaluationVariant
            },
        )
        evaluation_id = await runner.start(
            phase=EvaluationPhase.PILOT,
            model="openai/gpt-oss-20b",
            git_commit="a" * 40,
            preflight_manifest_digest="b" * 64,
        )
        persisted = await repository.get(evaluation_id)
        await engine.dispose()
        assert persisted is not None
        return persisted.config.get("preflight_manifest_digest")

    assert asyncio.run(exercise()) == "b" * 64
