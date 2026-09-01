"""Importação redigida da revisão pela interface pública do módulo."""

import csv
import json
from pathlib import Path

import pytest
from indusguard_api.agent import AgentDecision

from indusguard_evals.contracts import (
    EvaluationExecutionKind,
    EvaluationPhase,
    EvaluationSample,
    EvaluationVariant,
    ScheduledRun,
)
from indusguard_evals.human_review import (
    HumanReviewImportError,
    ReviewMethod,
    import_human_review,
)
from indusguard_evals.repository import PersistedEvaluationRun
from tests.factories import agent_result

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RUBRIC_PATH = REPOSITORY_ROOT / "evals" / "rubrics" / "judge.yaml"
DIMS = [
    "evidence_fidelity_0_or_1",
    "uncertainty_honesty_0_or_1",
    "justification_quality_0_or_1",
    "clarity_relevance_0_or_1",
]


def _samples() -> list[EvaluationSample]:
    samples = []
    for index, (variant, seed) in enumerate(
        ((EvaluationVariant.PROMPT_ONLY, 11), (EvaluationVariant.GUARDED, 42)), 1
    ):
        result = agent_result(decision=AgentDecision.ORIENT).model_copy(
            update={"run_id": f"00000000-0000-4000-8000-{index:012d}"}
        )
        samples.append(
            EvaluationSample(
                scheduled=ScheduledRun(
                    case_id="case_tkt_inv_04",
                    scenario_id="CEN-01",
                    variant=variant,
                    seed=seed,
                    ordinal=index,
                ),
                result=result,
            )
        )
    return samples


def _run() -> PersistedEvaluationRun:
    return PersistedEvaluationRun(
        evaluation_id="11111111-1111-4111-8111-111111111111",
        phase=EvaluationPhase.PILOT,
        status="completed",
        dataset_version="official-v1",
        input_digest="a" * 64,
        golden_digest="b" * 64,
        model="openai/gpt-oss-20b",
        git_commit="c" * 40,
        config={"execution_kind": EvaluationExecutionKind.GROQ_PILOT.value},
        summary={"status": "completed", "expected_runs": 2, "completed_runs": 2},
    )


def _write_review(tmp_path: Path, samples: list[EvaluationSample]) -> tuple[Path, Path]:
    csv_path = tmp_path / "review.csv"
    key_path = tmp_path / "review-key.json"
    aliases = ["sample-alpha", "sample-beta"]
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "sample_alias",
                "scenario_id",
                "user_message",
                "answer",
                "evidence",
                *DIMS,
                "reviewer_notes",
            ],
        )
        writer.writeheader()
        for alias, sample, score in zip(aliases, samples, ("1", "0"), strict=True):
            writer.writerow(
                {
                    "sample_alias": alias,
                    "scenario_id": sample.scheduled.scenario_id,
                    "user_message": "mensagem que não pode sair no bundle",
                    "answer": "resposta que não pode sair no bundle",
                    "evidence": '[{"segredo": "não copiar"}]',
                    **{dimension: score for dimension in DIMS},
                    "reviewer_notes": "nota livre que também fica no CSV",
                }
            )
    key_path.write_text(
        json.dumps(
            {
                alias: {
                    "case_id": sample.scheduled.case_id,
                    "variant": sample.scheduled.variant.value,
                    "seed": sample.scheduled.seed,
                    "agent_run_id": sample.result.run_id,
                }
                for alias, sample in zip(aliases, samples, strict=True)
            }
        ),
        encoding="utf-8",
    )
    return csv_path, key_path


def test_review_import_builds_auditable_redacted_bundle(tmp_path: Path) -> None:
    samples = _samples()
    csv_path, key_path = _write_review(tmp_path, samples)

    bundle = import_human_review(
        _run(),
        samples,
        csv_path=csv_path,
        key_path=key_path,
        rubric_path=RUBRIC_PATH,
        review_method=ReviewMethod.ASSISTED,
    )
    serialized = bundle.model_dump_json()

    assert bundle.schema_version == "human-review-bundle-v1"
    assert bundle.review_method is ReviewMethod.ASSISTED
    assert bundle.calibrated is False
    assert bundle.sample_count == 2
    assert bundle.aggregates["evidence_fidelity"] == 0.5
    assert len(bundle.source_csv_sha256) == 64
    assert len(bundle.key_sha256) == 64
    assert len(bundle.rubric_sha256) == 64
    assert "mensagem que não pode sair" not in serialized
    assert "resposta que não pode sair" not in serialized
    assert "segredo" not in serialized
    assert "nota livre" not in serialized


@pytest.mark.parametrize(
    "mutation",
    ["unknown_alias", "invalid_score", "duplicate_alias", "wrong_key"],
)
def test_review_import_rejects_invalid_or_mismatched_artifacts(
    tmp_path: Path,
    mutation: str,
) -> None:
    samples = _samples()
    csv_path, key_path = _write_review(tmp_path, samples)
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8", newline="")))
    if mutation == "unknown_alias":
        rows[0]["sample_alias"] = "sample-unknown"
    elif mutation == "invalid_score":
        rows[0][DIMS[0]] = "2"
    elif mutation == "duplicate_alias":
        rows[1]["sample_alias"] = rows[0]["sample_alias"]
    else:
        key = json.loads(key_path.read_text(encoding="utf-8"))
        key["sample-alpha"]["agent_run_id"] = "99999999-9999-4999-8999-999999999999"
        key_path.write_text(json.dumps(key), encoding="utf-8")
    if mutation != "wrong_key":
        with csv_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=rows[0])
            writer.writeheader()
            writer.writerows(rows)

    with pytest.raises(HumanReviewImportError, match="HUMAN_REVIEW_INVALID"):
        import_human_review(
            _run(),
            samples,
            csv_path=csv_path,
            key_path=key_path,
            rubric_path=RUBRIC_PATH,
            review_method=ReviewMethod.HUMAN,
        )
