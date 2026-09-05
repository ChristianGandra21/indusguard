"""Comandos de melhoria exercitados pela interface pública do CLI e SQLite real."""

import asyncio
import csv
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from indusguard_api.agent import AgentDecision, AgentRunRequest
from indusguard_api.connectors import ConnectorCatalog
from indusguard_api.persistence import Base, SqlAlchemyAgentRunRecorder
from sqlalchemy.ext.asyncio import create_async_engine

from indusguard_evals.analysis import FailureCategory, ImprovementFinding, ImprovementPlan
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
from indusguard_evals.improvement import ImprovementPatchError, ImprovementPatchWriter
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


def test_improve_cli_writes_json_output_for_future_ui(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'eval.db'}"
    evaluation_id, _ = asyncio.run(_seed_evaluation(database_url))
    output = tmp_path / "artifacts" / "improvement-plan.md"
    json_output = tmp_path / "artifacts" / "improvement-plan.json"

    assert (
        main(
            [
                "--database-url",
                database_url,
                "improve",
                evaluation_id,
                "--output",
                str(output),
                "--json-output",
                str(json_output),
            ]
        )
        == 0
    )

    payload = json.loads(json_output.read_text(encoding="utf-8"))
    markdown = output.read_text(encoding="utf-8")
    assert payload["schema_version"] == "improvement-plan-v1"
    assert payload["evaluation_id"] == evaluation_id
    assert evaluation_id in markdown
    assert "patch_result" not in payload


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
                "--write-patch",
            ]
        )

    assert not output.exists()


def test_patch_writer_applies_only_allowlisted_recipes_in_clean_matching_checkout(
    tmp_path: Path,
) -> None:
    root = _seed_patch_repo(tmp_path)
    head = _git(root, "rev-parse", "HEAD")

    result = ImprovementPatchWriter(root).apply(_patchable_plan(head))

    assert result.schema_version == "improvement-patch-v1"
    assert {recipe.name for recipe in result.recipes} == {
        "agent-guidance-recipe",
        "baseline-contrast-recipe",
    }
    assert set(result.changed_files) == {
        "connectors/tractian/domain.yaml",
        "evals/src/indusguard_evals/execution.py",
    }
    assert "analysisId deve aparecer em evidência observada" in (
        root / "connectors" / "tractian" / "domain.yaml"
    ).read_text(encoding="utf-8")
    assert "restrict_tools_to_intent" in (
        root / "evals" / "src" / "indusguard_evals" / "execution.py"
    ).read_text(encoding="utf-8")
    assert (root / "evals" / "corpus" / "official-v1" / "goldens" / "scenarios.yaml").read_text(
        encoding="utf-8"
    ) == "golden: intacto\n"


def test_patch_writer_rejects_dirty_checkout_and_commit_mismatch(tmp_path: Path) -> None:
    dirty = _seed_patch_repo(tmp_path / "dirty")
    (dirty / "connectors" / "tractian" / "domain.yaml").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(ImprovementPatchError, match="IMPROVEMENT_PATCH_DIRTY_WORKTREE"):
        ImprovementPatchWriter(dirty).apply(_patchable_plan(_git(dirty, "rev-parse", "HEAD")))

    mismatch = _seed_patch_repo(tmp_path / "mismatch")
    with pytest.raises(ImprovementPatchError, match="IMPROVEMENT_PATCH_COMMIT_MISMATCH"):
        ImprovementPatchWriter(mismatch).apply(_patchable_plan("b" * 40))


def test_patch_writer_blocks_forbidden_paths(tmp_path: Path) -> None:
    root = _seed_patch_repo(tmp_path)
    writer = ImprovementPatchWriter(root)

    for forbidden in (
        ".env",
        "deploy/api.Dockerfile",
        "evals/corpus/official-v1/inputs.json",
        "evals/corpus/official-v1/goldens/scenarios.yaml",
    ):
        with pytest.raises(ImprovementPatchError, match="IMPROVEMENT_PATCH_PATH_FORBIDDEN"):
            writer._safe_path(forbidden)


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


def _seed_patch_repo(root: Path) -> Path:
    (root / "connectors" / "tractian").mkdir(parents=True)
    (root / "evals" / "src" / "indusguard_evals").mkdir(parents=True)
    (root / "evals" / "corpus" / "official-v1" / "goldens").mkdir(parents=True)
    (root / "deploy").mkdir()
    (root / "connectors" / "tractian" / "domain.yaml").write_text(
        "intents:\n"
        "  - id: agir\n"
        "    description: Para requestSpecialistAnalysis, nunca use case_id, "
        "asset_id ou id de baseline como analysisId.\n",
        encoding="utf-8",
    )
    (root / "evals" / "src" / "indusguard_evals" / "execution.py").write_text(
        "def create_variant_runtime(runtime_config):\n"
        "    runtime = AgentRuntime(\n"
        "        catalog,\n"
        "        probe,\n"
        "        model_gateway,\n"
        "        recorder=recorder,\n"
        "        config=runtime_config,\n"
        "    )\n",
        encoding="utf-8",
    )
    (root / "evals" / "corpus" / "official-v1" / "goldens" / "scenarios.yaml").write_text(
        "golden: intacto\n",
        encoding="utf-8",
    )
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (root / "deploy" / "api.Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")
    return root


def _patchable_plan(git_commit: str) -> ImprovementPlan:
    finding = ImprovementFinding(
        case_id="case_tkt_exe_13",
        scenario_id="CEN-14",
        variant="guarded",
        seed=42,
        categories=[
            FailureCategory.MISSING_EVIDENCE,
            FailureCategory.EXPECTED_ACTION_MISSING,
        ],
        allowed_decisions=["act"],
        actual_decision="orient",
        expected_operations=["listAnalyses", "getAnalysis", "getBaseline"],
        actual_operations=["listAnalyses"],
        missing_operations=["getAnalysis", "getBaseline"],
        unexpected_operations=[],
        expected_action="requestSpecialistAnalysis",
        actual_actions=[],
        termination_reason="COMPLETED",
    )
    return ImprovementPlan(
        generated_at=datetime.now(UTC),
        evaluation_id="22222222-2222-4222-8222-222222222222",
        phase="pilot",
        execution_kind="gemini_pilot",
        dataset_version="official-v1",
        input_digest="a" * 64,
        golden_digest="b" * 64,
        model="gemini:test",
        git_commit=git_commit,
        analyzed_runs=1,
        benchmark_criteria={"prompt_only_more_unsafe_than_guarded": False},
        findings=[finding],
        failure_clusters=[],
        recommendations=[],
        limitations=[],
    )


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args],
        cwd=root,
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()


def test_prepare_cli_rejects_invalid_eval_before_creating_proposal(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'eval.db'}"
    evaluation_id, _ = asyncio.run(_seed_evaluation(database_url, status="invalid"))
    directory = tmp_path / "proposals"
    with pytest.raises(SystemExit, match="EVALUATION_NOT_ANALYZABLE"):
        main(
            [
                "--database-url",
                database_url,
                "improvement-prepare",
                evaluation_id,
                "--improvements-dir",
                str(directory),
            ]
        )
    assert not directory.exists()
