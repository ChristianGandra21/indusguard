"""Checkpoint transacional do benchmark pela interface pública do repositório."""

import asyncio
from pathlib import Path

from indusguard_api.agent import AgentDecision, AgentRunRequest
from indusguard_api.persistence import Base, SqlAlchemyAgentRunRecorder
from sqlalchemy.ext.asyncio import create_async_engine

from indusguard_evals.contracts import (
    CaseScore,
    EvaluationPhase,
    EvaluationSample,
    EvaluationVariant,
    ScheduledRun,
)
from indusguard_evals.repository import EvaluationRepository
from tests.factories import agent_result


def test_checkpoint_is_idempotent_and_resume_reads_completed_identity(tmp_path: Path) -> None:
    async def exercise() -> tuple[set[tuple[str, EvaluationVariant, int]], int, str]:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'eval.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        agent_recorder = SqlAlchemyAgentRunRecorder(engine)
        repository = EvaluationRepository(engine)
        result = agent_result(decision=AgentDecision.ORIENT)
        await agent_recorder.record(
            request=AgentRunRequest(connector_id="tractian", message="mensagem", seed=42),
            result=result,
        )
        evaluation_id = await repository.start(
            phase=EvaluationPhase.PILOT,
            dataset_version="official-v1",
            input_digest="a" * 64,
            model="fake-eval-model",
            git_commit="abc123",
            config={"seeds": [42]},
        )
        scheduled = ScheduledRun(
            case_id="case_tkt_inv_07",
            scenario_id="CEN-05",
            variant=EvaluationVariant.GUARDED,
            seed=42,
            ordinal=0,
        )
        sample = EvaluationSample(scheduled=scheduled, result=result)
        score = CaseScore(
            case_id=scheduled.case_id,
            scenario_id=scheduled.scenario_id,
            variant=scheduled.variant,
            seed=scheduled.seed,
            decision_correct=True,
            task_success=True,
            safe_success=True,
            tool_precision=1,
            tool_recall=1,
            evidence_coverage=1,
            argument_accuracy=1,
            citation_validity=1,
            redundant_calls=0,
            unsafe_writes_reaching_executor=0,
            structurally_valid_writes=0,
            proposed_writes=0,
        )

        await repository.checkpoint(evaluation_id, sample)
        await repository.checkpoint(evaluation_id, sample)
        # O gabarito só seria aberto agora; score é uma segunda fase explícita.
        await repository.apply_score(evaluation_id, score)
        completed = await repository.completed_identities(evaluation_id)
        stored = await repository.results(evaluation_id)
        await repository.finish(evaluation_id, status="partial", summary={"completed": 1})
        run = await repository.get(evaluation_id)
        await engine.dispose()
        assert run is not None
        return completed, len(stored), run.status

    completed, result_count, status = asyncio.run(exercise())

    assert completed == {("case_tkt_inv_07", EvaluationVariant.GUARDED, 42)}
    assert result_count == 1
    assert status == "partial"
