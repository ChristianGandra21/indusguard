"""Comparação auditável exercitada pela interface pública do módulo."""

from pathlib import Path

import pytest
from indusguard_api.agent import AgentDecision
from indusguard_api.connectors import ConnectorCatalog

from indusguard_evals.comparison import EvaluationComparator, EvaluationComparisonError
from indusguard_evals.contracts import (
    EvaluationExecutionKind,
    EvaluationPhase,
    EvaluationSample,
    EvaluationVariant,
    ScheduledRun,
)
from indusguard_evals.corpus import OfficialCorpus
from indusguard_evals.repository import PersistedEvaluationRun
from tests.factories import agent_result, evidence, tool_call

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CORPUS_ROOT = REPOSITORY_ROOT / "evals" / "corpus" / "official-v1"


def _comparator() -> tuple[EvaluationComparator, object, object]:
    corpus = OfficialCorpus(CORPUS_ROOT)
    inputs = corpus.load_inputs()
    goldens = corpus.load_goldens(inputs)
    catalog = ConnectorCatalog(REPOSITORY_ROOT / "connectors")
    catalog.load()
    return EvaluationComparator(catalog, inputs, goldens), inputs, goldens


def _run(
    evaluation_id: str,
    commit: str,
    inputs: object,
    goldens: object,
) -> PersistedEvaluationRun:
    return PersistedEvaluationRun(
        evaluation_id=evaluation_id,
        phase=EvaluationPhase.PILOT,
        status="completed",
        dataset_version=inputs.version,
        input_digest=inputs.digest,
        golden_digest=goldens.digest,
        model="openai/gpt-oss-20b",
        git_commit=commit,
        config={"execution_kind": EvaluationExecutionKind.GROQ_PILOT.value},
        summary={
            "status": "completed",
            "expected_runs": 1,
            "completed_runs": 1,
            "runtime_failures": {},
        },
    )


def _sample(
    *,
    decision: AgentDecision,
    operations: list[str],
    latency_ms: float,
) -> EvaluationSample:
    calls = [
        tool_call(operation, evidence_id=f"ev-{index:03d}")
        for index, operation in enumerate(operations, 1)
    ]
    collected = [
        evidence(operation, evidence_id=f"ev-{index:03d}")
        for index, operation in enumerate(operations, 1)
    ]
    return EvaluationSample(
        scheduled=ScheduledRun(
            case_id="case_tkt_inv_04",
            scenario_id="CEN-01",
            variant=EvaluationVariant.PROMPT_ONLY,
            seed=11,
            ordinal=0,
        ),
        result=agent_result(
            decision=decision,
            tool_calls=calls,
            evidence=collected,
            latency_ms=latency_ms,
        ),
    )


def test_comparator_reports_known_improvement_without_sensitive_payloads() -> None:
    comparator, inputs, goldens = _comparator()
    baseline = _run("11111111-1111-4111-8111-111111111111", "a" * 40, inputs, goldens)
    candidate = _run("22222222-2222-4222-8222-222222222222", "b" * 40, inputs, goldens)
    baseline_sample = _sample(
        decision=AgentDecision.ORIENT,
        operations=["getAsset", "getDataQuality"],
        latency_ms=100,
    )
    candidate_sample = _sample(
        decision=AgentDecision.ESCALATE,
        operations=["getAsset", "getBaseline", "getDataQuality", "getRmsSeries"],
        latency_ms=80,
    )

    comparison = comparator.compare(
        baseline,
        [baseline_sample],
        candidate,
        [candidate_sample],
    )
    rendered = comparison.to_markdown()

    assert comparison.schema_version == "evaluation-comparison-v1"
    assert comparison.utility.task_success_rate.model_dump() == {
        "baseline": 0.0,
        "candidate": 1.0,
        "delta": 1.0,
    }
    assert comparison.utility.evidence_coverage.delta == 0.5
    assert comparison.runtime.median_latency_ms.delta == -20.0
    assert comparison.identities[0].outcome == "improved"
    assert {(change.category.value, change.status) for change in comparison.failure_changes} >= {
        ("decision_incorrect", "resolved"),
        ("missing_evidence", "resolved"),
    }
    assert "Resposta fundamentada" not in rendered
    assert '"execution"' not in rendered


def test_comparator_rejects_same_evaluation() -> None:
    comparator, inputs, goldens = _comparator()
    run = _run("11111111-1111-4111-8111-111111111111", "a" * 40, inputs, goldens)
    sample = _sample(
        decision=AgentDecision.ORIENT,
        operations=["getAsset", "getDataQuality"],
        latency_ms=100,
    )

    with pytest.raises(EvaluationComparisonError, match="EVALUATION_COMPARISON_INVALID"):
        comparator.compare(run, [sample], run, [sample])


def test_comparator_rejects_incompatible_schedule() -> None:
    comparator, inputs, goldens = _comparator()
    baseline = _run("11111111-1111-4111-8111-111111111111", "a" * 40, inputs, goldens)
    candidate = _run("22222222-2222-4222-8222-222222222222", "b" * 40, inputs, goldens)
    baseline_sample = _sample(
        decision=AgentDecision.ORIENT,
        operations=["getAsset", "getDataQuality"],
        latency_ms=100,
    )
    candidate_sample = _sample(
        decision=AgentDecision.ESCALATE,
        operations=["getAsset", "getBaseline", "getDataQuality", "getRmsSeries"],
        latency_ms=80,
    )
    candidate_sample.scheduled = ScheduledRun(
        case_id="case_tkt_inv_04",
        scenario_id="CEN-01",
        variant=EvaluationVariant.PROMPT_ONLY,
        seed=29,
        ordinal=0,
    )

    with pytest.raises(EvaluationComparisonError, match="EVALUATION_COMPARISON_MISMATCH"):
        comparator.compare(baseline, [baseline_sample], candidate, [candidate_sample])


@pytest.mark.parametrize(
    ("run_update", "error_code"),
    [
        ({"status": "partial"}, "EVALUATION_NOT_COMPARABLE"),
        (
            {"config": {"execution_kind": EvaluationExecutionKind.OFFLINE_SMOKE.value}},
            "EVALUATION_NOT_COMPARABLE",
        ),
        (
            {
                "summary": {
                    "status": "completed",
                    "expected_runs": 1,
                    "completed_runs": 1,
                    "runtime_failures": {"MODEL_RATE_LIMITED": 1},
                }
            },
            "EVALUATION_NOT_COMPARABLE",
        ),
        ({"input_digest": "0" * 64}, "EVALUATION_ARTIFACT_MISMATCH"),
    ],
)
def test_comparator_rejects_ineligible_evaluation_with_stable_code(
    run_update: dict[str, object], error_code: str
) -> None:
    comparator, inputs, goldens = _comparator()
    baseline = _run("11111111-1111-4111-8111-111111111111", "a" * 40, inputs, goldens)
    candidate = _run("22222222-2222-4222-8222-222222222222", "b" * 40, inputs, goldens)
    candidate = candidate.model_copy(update=run_update)
    sample = _sample(
        decision=AgentDecision.ORIENT,
        operations=["getAsset", "getDataQuality"],
        latency_ms=100,
    )

    with pytest.raises(EvaluationComparisonError, match=error_code):
        comparator.compare(baseline, [sample], candidate, [sample])


def test_comparator_rejects_different_model() -> None:
    comparator, inputs, goldens = _comparator()
    baseline = _run("11111111-1111-4111-8111-111111111111", "a" * 40, inputs, goldens)
    candidate = _run("22222222-2222-4222-8222-222222222222", "b" * 40, inputs, goldens)
    candidate = candidate.model_copy(update={"model": "different-model"})
    sample = _sample(
        decision=AgentDecision.ORIENT,
        operations=["getAsset", "getDataQuality"],
        latency_ms=100,
    )

    with pytest.raises(EvaluationComparisonError, match="EVALUATION_COMPARISON_MISMATCH"):
        comparator.compare(baseline, [sample], candidate, [sample])


def test_comparator_groups_persistent_and_new_failures_across_identities() -> None:
    comparator, inputs, goldens = _comparator()
    completed_summary = {
        "status": "completed",
        "expected_runs": 2,
        "completed_runs": 2,
        "runtime_failures": {},
    }
    baseline = _run("11111111-1111-4111-8111-111111111111", "a" * 40, inputs, goldens).model_copy(
        update={"summary": completed_summary}
    )
    candidate = _run("22222222-2222-4222-8222-222222222222", "b" * 40, inputs, goldens).model_copy(
        update={"summary": completed_summary}
    )
    baseline_samples = [
        _sample(
            decision=AgentDecision.ORIENT,
            operations=["getAsset", "getDataQuality"],
            latency_ms=100,
        )
        for _ in range(2)
    ]
    candidate_samples = [
        _sample(
            decision=AgentDecision.ESCALATE,
            operations=["getAsset", "getDataQuality", "getCompany"],
            latency_ms=90,
        )
        for _ in range(2)
    ]
    for samples in (baseline_samples, candidate_samples):
        samples[1].scheduled = ScheduledRun(
            case_id="case_tkt_inv_04",
            scenario_id="CEN-01",
            variant=EvaluationVariant.GUARDED,
            seed=29,
            ordinal=1,
        )

    comparison = comparator.compare(
        baseline,
        baseline_samples,
        candidate,
        candidate_samples,
    )
    changes = {
        change.category.value: (change.status, change.baseline_count, change.candidate_count)
        for change in comparison.failure_changes
    }

    assert changes["decision_incorrect"] == ("resolved", 2, 0)
    assert changes["missing_evidence"] == ("persistent", 2, 2)
    assert changes["unexpected_tool"] == ("new", 0, 2)
    assert len(comparison.identities) == 2
