"""Comando compare exercitado pela interface pública do CLI e SQLite real."""

import asyncio
from pathlib import Path

import pytest
from indusguard_api.agent import AgentDecision, AgentRunRequest
from indusguard_api.persistence import Base, SqlAlchemyAgentRunRecorder
from sqlalchemy.ext.asyncio import create_async_engine

from indusguard_evals.cli import main
from indusguard_evals.contracts import (
    EvaluationExecutionKind,
    EvaluationPhase,
    EvaluationSample,
    EvaluationVariant,
    ScheduledRun,
)
from indusguard_evals.corpus import OfficialCorpus
from indusguard_evals.repository import EvaluationRepository
from tests.factories import agent_result, evidence, tool_call

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CORPUS_ROOT = REPOSITORY_ROOT / "evals" / "corpus" / "official-v1"


async def _seed_pair(database_url: str) -> tuple[str, str]:
    corpus = OfficialCorpus(CORPUS_ROOT)
    inputs = corpus.load_inputs()
    goldens = corpus.load_goldens(inputs)
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    recorder = SqlAlchemyAgentRunRecorder(engine)
    repository = EvaluationRepository(engine)
    evaluation_ids: list[str] = []
    specifications = [
        (
            "00000000-0000-4000-8000-000000000011",
            AgentDecision.ORIENT,
            ["getAsset", "getDataQuality"],
            "a" * 40,
            100.0,
        ),
        (
            "00000000-0000-4000-8000-000000000022",
            AgentDecision.ESCALATE,
            ["getAsset", "getBaseline", "getDataQuality", "getRmsSeries"],
            "b" * 40,
            80.0,
        ),
    ]
    for run_id, decision, operations, git_commit, latency_ms in specifications:
        calls = [
            tool_call(operation, evidence_id=f"ev-{index:03d}")
            for index, operation in enumerate(operations, 1)
        ]
        collected = [
            evidence(operation, evidence_id=f"ev-{index:03d}")
            for index, operation in enumerate(operations, 1)
        ]
        result = agent_result(
            decision=decision,
            tool_calls=calls,
            evidence=collected,
            latency_ms=latency_ms,
        ).model_copy(update={"run_id": run_id})
        sample = EvaluationSample(
            scheduled=ScheduledRun(
                case_id="case_tkt_inv_04",
                scenario_id="CEN-01",
                variant=EvaluationVariant.GUARDED,
                seed=42,
                ordinal=0,
            ),
            result=result,
        )
        await recorder.record(
            request=AgentRunRequest(
                connector_id="tractian",
                message="mensagem sintética",
                seed=42,
            ),
            result=result,
        )
        evaluation_id = await repository.start(
            phase=EvaluationPhase.PILOT,
            dataset_version=inputs.version,
            input_digest=inputs.digest,
            model="openai/gpt-oss-20b",
            git_commit=git_commit,
            config={"execution_kind": EvaluationExecutionKind.GROQ_PILOT.value},
        )
        await repository.checkpoint(evaluation_id, sample)
        await repository.finish(
            evaluation_id,
            status="completed",
            summary={
                "status": "completed",
                "expected_runs": 1,
                "completed_runs": 1,
                "runtime_failures": {},
            },
            golden_digest=goldens.digest,
        )
        evaluation_ids.append(evaluation_id)
    await engine.dispose()
    return evaluation_ids[0], evaluation_ids[1]


def test_compare_cli_writes_only_the_requested_redacted_markdown(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'eval.db'}"
    baseline_id, candidate_id = asyncio.run(_seed_pair(database_url))
    output = tmp_path / "artifacts" / "comparison.md"

    assert (
        main(
            [
                "--database-url",
                database_url,
                "compare",
                baseline_id,
                candidate_id,
                "--output",
                str(output),
            ]
        )
        == 0
    )

    rendered = output.read_text(encoding="utf-8")
    assert "evaluation-comparison-v1" in rendered
    assert "Resposta fundamentada" not in rendered
    assert '"execution"' not in rendered
    assert list(tmp_path.rglob("*.md")) == [output]


def test_compare_cli_rejects_unknown_or_same_evaluation_without_writing(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'eval.db'}"
    baseline_id, _ = asyncio.run(_seed_pair(database_url))

    with pytest.raises(SystemExit, match="EVALUATION_NOT_FOUND"):
        main(
            [
                "--database-url",
                database_url,
                "compare",
                "99999999-9999-4999-8999-999999999999",
                baseline_id,
                "--output",
                str(tmp_path / "missing.md"),
            ]
        )
    with pytest.raises(SystemExit, match="EVALUATION_COMPARISON_INVALID"):
        main(
            [
                "--database-url",
                database_url,
                "compare",
                baseline_id,
                baseline_id,
                "--output",
                str(tmp_path / "same.md"),
            ]
        )

    assert not (tmp_path / "missing.md").exists()
    assert not (tmp_path / "same.md").exists()
