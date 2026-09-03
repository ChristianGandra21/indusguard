"""Checkpoint transacional do benchmark pela interface pública do repositório."""

import asyncio
from pathlib import Path

from indusguard_api.agent import AgentDecision, AgentRunRequest, AgentTerminationReason
from indusguard_api.persistence import Base, SqlAlchemyAgentRunRecorder
from sqlalchemy.ext.asyncio import create_async_engine

from indusguard_evals.contracts import (
    CaseScore,
    EvaluationPhase,
    EvaluationSample,
    EvaluationVariant,
    ModelProviderAttempt,
    ScheduledRun,
)
from indusguard_evals.repository import EvaluationRepository
from tests.factories import agent_result


def test_checkpoint_is_idempotent_and_resume_reads_completed_identity(tmp_path: Path) -> None:
    async def exercise() -> tuple[set[tuple[str, EvaluationVariant, int]], int, str, str]:
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
        sample = EvaluationSample(
            scheduled=scheduled,
            result=result,
            model_provider_attempts=[
                ModelProviderAttempt(
                    model="gemini:gemini-3.7-flash",
                    agent_run_id=result.run_id,
                    termination_reason="COMPLETED",
                )
            ],
        )
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
        stored_samples = await repository.samples(evaluation_id)
        await repository.finish(evaluation_id, status="partial", summary={"completed": 1})
        run = await repository.get(evaluation_id)
        await engine.dispose()
        assert run is not None
        return (
            completed,
            len(stored),
            run.status,
            stored_samples[0].model_provider_attempts[0].model,
        )

    completed, result_count, status, attempted_model = asyncio.run(exercise())

    assert completed == {("case_tkt_inv_07", EvaluationVariant.GUARDED, 42)}
    assert result_count == 1
    assert status == "partial"
    assert attempted_model == "gemini:gemini-3.7-flash"


def test_retryable_checkpoint_appends_provider_attempts_across_resume(tmp_path: Path) -> None:
    async def exercise() -> list[str]:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'resume.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        recorder = SqlAlchemyAgentRunRecorder(engine)
        repository = EvaluationRepository(engine)
        evaluation_id = await repository.start(
            phase=EvaluationPhase.PILOT,
            dataset_version="official-v1",
            input_digest="a" * 64,
            model="fallback-chain",
            git_commit="abc123",
            config={"seeds": [11]},
        )
        scheduled = ScheduledRun(
            case_id="case_tkt_inv_04",
            scenario_id="CEN-01",
            variant=EvaluationVariant.GUARDED,
            seed=11,
            ordinal=0,
        )
        attempts = [
            (
                "00000000-0000-0000-0000-000000000011",
                AgentTerminationReason.MODEL_RATE_LIMITED,
                "openai/gpt-oss-20b",
            ),
            (
                "00000000-0000-0000-0000-000000000012",
                AgentTerminationReason.COMPLETED,
                "gemini:gemini-3.7-flash",
            ),
        ]
        for run_id, termination, model in attempts:
            result = agent_result(termination=termination).model_copy(update={"run_id": run_id})
            result = result.model_copy(
                update={"metrics": result.metrics.model_copy(update={"model": model})}
            )
            await recorder.record(
                request=AgentRunRequest(connector_id="tractian", message="mensagem", seed=11),
                result=result,
            )
            await repository.checkpoint(
                evaluation_id,
                EvaluationSample(
                    scheduled=scheduled,
                    result=result,
                    model_provider_attempts=[
                        ModelProviderAttempt(
                            model=model,
                            agent_run_id=run_id,
                            termination_reason=termination.value,
                        )
                    ],
                ),
            )
        samples = await repository.samples(evaluation_id)
        await engine.dispose()
        return [item.agent_run_id for item in samples[0].model_provider_attempts]

    assert asyncio.run(exercise()) == [
        "00000000-0000-0000-0000-000000000011",
        "00000000-0000-0000-0000-000000000012",
    ]
