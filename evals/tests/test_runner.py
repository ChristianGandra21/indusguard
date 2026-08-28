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
from indusguard_evals.runner import BenchmarkRunner
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
    ) -> None:
        self.variant = variant
        self._recorder = recorder
        self._rate_limit_once = rate_limit_once

    async def run(self, scheduled: object, case: object) -> EvaluationSample:
        termination = (
            AgentTerminationReason.MODEL_RATE_LIMITED
            if self._rate_limit_once
            else AgentTerminationReason.COMPLETED
        )
        self._rate_limit_once = False
        result = agent_result(termination=termination).model_copy(update={"run_id": str(uuid4())})
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
    async def exercise() -> tuple[BenchmarkSummary, BenchmarkSummary, int]:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runner.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        repository = EvaluationRepository(engine)
        recorder = SqlAlchemyAgentRunRecorder(engine)
        catalog = ConnectorCatalog(REPOSITORY_ROOT / "connectors")
        catalog.load()
        corpus = OfficialCorpus(CORPUS_ROOT)
        first_runner = BenchmarkRunner(
            corpus=corpus,
            catalog=catalog,
            repository=repository,
            runtimes={
                EvaluationVariant.PROMPT_ONLY: FakeVariantRuntime(
                    EvaluationVariant.PROMPT_ONLY,
                    recorder,
                    rate_limit_once=True,
                ),
                EvaluationVariant.GUARDED: FakeVariantRuntime(
                    EvaluationVariant.GUARDED,
                    recorder,
                ),
            },
        )
        evaluation_id = await first_runner.start(
            phase=EvaluationPhase.PILOT,
            model="fake-eval-model",
            git_commit="abc123",
        )
        partial = await first_runner.execute(evaluation_id)
        resumed_runner = BenchmarkRunner(
            corpus=corpus,
            catalog=catalog,
            repository=repository,
            runtimes={
                variant: FakeVariantRuntime(variant, recorder) for variant in EvaluationVariant
            },
        )
        completed = await resumed_runner.execute(evaluation_id)
        identities = await repository.completed_identities(evaluation_id)
        await engine.dispose()
        return partial, completed, len(identities)

    partial, completed, identity_count = asyncio.run(exercise())

    assert partial.status == "partial"
    assert partial.completed_runs == 0
    assert completed.status == "completed"
    assert completed.completed_runs == 12
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
