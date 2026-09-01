"""Comandos de melhoria exercitados pela interface pública do CLI e SQLite real."""

import asyncio
import csv
import json
from pathlib import Path

import pytest
from indusguard_api.agent import AgentDecision, AgentRunRequest
from indusguard_api.connectors import ConnectorCatalog
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
from indusguard_evals.human_review import export_human_review
from indusguard_evals.repository import EvaluationRepository
from indusguard_evals.scorer import DeterministicScorer
from tests.factories import agent_result, evidence, tool_call

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CORPUS_ROOT = REPOSITORY_ROOT / "evals" / "corpus" / "official-v1"


async def _seed_evaluation(
    database_url: str,
    *,
    status: str = "completed",
    execution_kind: EvaluationExecutionKind = EvaluationExecutionKind.GROQ_PILOT,
    expected_runs: int = 1,
    golden_digest: str | None = None,
) -> tuple[str, EvaluationSample]:
    corpus = OfficialCorpus(CORPUS_ROOT)
    inputs = corpus.load_inputs()
    goldens = corpus.load_goldens(inputs)
    catalog = ConnectorCatalog(REPOSITORY_ROOT / "connectors")
    catalog.load()
    operations = ["getAsset", "getBaseline", "getDataQuality", "getRmsSeries"]
    calls = [
        tool_call(operation, evidence_id=f"ev-{index:03d}")
        for index, operation in enumerate(operations, 1)
    ]
    collected = [
        evidence(operation, evidence_id=f"ev-{index:03d}")
        for index, operation in enumerate(operations, 1)
    ]
    sample = EvaluationSample(
        scheduled=ScheduledRun(
            case_id="case_tkt_inv_04",
            scenario_id="CEN-01",
            variant=EvaluationVariant.GUARDED,
            seed=42,
            ordinal=0,
        ),
        result=agent_result(
            decision=AgentDecision.ESCALATE,
            tool_calls=calls,
            evidence=collected,
        ),
    )
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    recorder = SqlAlchemyAgentRunRecorder(engine)
    repository = EvaluationRepository(engine)
    await recorder.record(
        request=AgentRunRequest(connector_id="tractian", message="mensagem sintética", seed=42),
        result=sample.result,
    )
    evaluation_id = await repository.start(
        phase=EvaluationPhase.PILOT,
        dataset_version=inputs.version,
        input_digest=inputs.digest,
        model="openai/gpt-oss-20b",
        git_commit="a" * 40,
        config={"execution_kind": execution_kind.value},
    )
    await repository.checkpoint(evaluation_id, sample)
    score = DeterministicScorer(catalog, inputs, goldens).score(sample)
    await repository.apply_score(evaluation_id, score)
    await repository.finish(
        evaluation_id,
        status=status,
        summary={
            "status": status,
            "expected_runs": expected_runs,
            "completed_runs": 1,
            "runtime_failures": {},
        },
        golden_digest=golden_digest or goldens.digest,
    )
    await engine.dispose()
    return evaluation_id, sample


def test_improve_cli_writes_only_the_requested_markdown(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'eval.db'}"
    evaluation_id, _ = asyncio.run(_seed_evaluation(database_url))
    output = tmp_path / "artifacts" / "improvement-plan.md"

    assert (
        main(
            [
                "--database-url",
                database_url,
                "improve",
                evaluation_id,
                "--output",
                str(output),
            ]
        )
        == 0
    )

    assert "improvement-plan-v1" in output.read_text(encoding="utf-8")
    assert list(tmp_path.rglob("*.md")) == [output]


def test_improve_cli_rejects_unknown_and_partial_evaluations(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'eval.db'}"
    partial_id, _ = asyncio.run(_seed_evaluation(database_url, status="partial"))

    with pytest.raises(SystemExit, match="EVALUATION_NOT_FOUND"):
        main(
            [
                "--database-url",
                database_url,
                "improve",
                "99999999-9999-4999-8999-999999999999",
                "--output",
                str(tmp_path / "missing.md"),
            ]
        )
    with pytest.raises(SystemExit, match="EVALUATION_NOT_ANALYZABLE"):
        main(
            [
                "--database-url",
                database_url,
                "improve",
                partial_id,
                "--output",
                str(tmp_path / "partial.md"),
            ]
        )
    assert not (tmp_path / "missing.md").exists()
    assert not (tmp_path / "partial.md").exists()


@pytest.mark.parametrize(
    ("seed_options", "error_code"),
    [
        (
            {"execution_kind": EvaluationExecutionKind.OFFLINE_SMOKE},
            "EVALUATION_NOT_ANALYZABLE",
        ),
        ({"status": "invalid"}, "EVALUATION_NOT_ANALYZABLE"),
        ({"expected_runs": 2}, "EVALUATION_NOT_ANALYZABLE"),
        ({"golden_digest": "b" * 64}, "EVALUATION_ARTIFACT_MISMATCH"),
    ],
    ids=["fake", "invalid", "incomplete_checkpoints", "changed_golden"],
)
def test_improve_cli_rejects_ineligible_evidence_with_stable_codes(
    tmp_path: Path,
    seed_options: dict[str, object],
    error_code: str,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'eval.db'}"
    evaluation_id, _ = asyncio.run(_seed_evaluation(database_url, **seed_options))
    output = tmp_path / "must-not-exist.md"

    with pytest.raises(SystemExit, match=error_code):
        main(
            [
                "--database-url",
                database_url,
                "improve",
                evaluation_id,
                "--output",
                str(output),
            ]
        )

    assert not output.exists()


def test_review_import_cli_writes_a_redacted_assisted_bundle(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'eval.db'}"
    evaluation_id, sample = asyncio.run(_seed_evaluation(database_url))
    corpus = OfficialCorpus(CORPUS_ROOT)
    csv_path = tmp_path / "review.csv"
    key_path = export_human_review([sample], corpus.load_inputs(), csv_path)
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8", newline="")))
    for dimension in (
        "evidence_fidelity_0_or_1",
        "uncertainty_honesty_0_or_1",
        "justification_quality_0_or_1",
        "clarity_relevance_0_or_1",
    ):
        rows[0][dimension] = "1"
    rows[0]["reviewer_notes"] = "não copiar esta nota"
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    output = tmp_path / "human-review.json"

    assert (
        main(
            [
                "--database-url",
                database_url,
                "review-import",
                evaluation_id,
                "--input",
                str(csv_path),
                "--key",
                str(key_path),
                "--review-method",
                "assisted",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert payload["schema_version"] == "human-review-bundle-v1"
    assert payload["review_method"] == "assisted"
    assert payload["calibrated"] is False
    assert "não copiar esta nota" not in output.read_text(encoding="utf-8")
