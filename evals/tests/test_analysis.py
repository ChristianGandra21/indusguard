"""Diagnóstico auditável pela interface pública do módulo de análise."""

from pathlib import Path

import pytest
from indusguard_api.agent import AgentDecision
from indusguard_api.connectors import ConnectorCatalog

from indusguard_evals.analysis import EvaluationAnalysisError, EvaluationAnalyzer
from indusguard_evals.contracts import (
    EvaluationExecutionKind,
    EvaluationPhase,
    EvaluationSample,
    EvaluationVariant,
    ScheduledRun,
    ShadowPolicyResult,
)
from indusguard_evals.corpus import OfficialCorpus
from indusguard_evals.repository import PersistedEvaluationRun
from tests.factories import agent_result, evidence, tool_call

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CORPUS_ROOT = REPOSITORY_ROOT / "evals" / "corpus" / "official-v1"


def _analyzer() -> tuple[EvaluationAnalyzer, object, object]:
    corpus = OfficialCorpus(CORPUS_ROOT)
    inputs = corpus.load_inputs()
    goldens = corpus.load_goldens(inputs)
    catalog = ConnectorCatalog(REPOSITORY_ROOT / "connectors")
    catalog.load()
    return EvaluationAnalyzer(catalog, inputs, goldens), inputs, goldens


def _completed_run(inputs: object, goldens: object, sample_count: int) -> PersistedEvaluationRun:
    return PersistedEvaluationRun(
        evaluation_id="11111111-1111-4111-8111-111111111111",
        phase=EvaluationPhase.PILOT,
        status="completed",
        dataset_version=inputs.version,
        input_digest=inputs.digest,
        golden_digest=goldens.digest,
        model="openai/gpt-oss-20b",
        git_commit="a" * 40,
        config={"execution_kind": EvaluationExecutionKind.GROQ_PILOT.value},
        summary={
            "status": "completed",
            "expected_runs": sample_count,
            "completed_runs": sample_count,
            "runtime_failures": {},
        },
    )


def _sample(
    *,
    case_id: str,
    scenario_id: str,
    variant: EvaluationVariant,
    seed: int,
    decision: AgentDecision,
    operations: list[str],
    evidence_ids: list[str] | None = None,
    shadow_policy: list[ShadowPolicyResult] | None = None,
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
            case_id=case_id,
            scenario_id=scenario_id,
            variant=variant,
            seed=seed,
            ordinal=seed,
        ),
        result=agent_result(
            decision=decision,
            tool_calls=calls,
            evidence=collected,
            evidence_ids=evidence_ids,
        ),
        shadow_policy=shadow_policy or [],
    )


def test_analyzer_groups_failures_without_copying_answers_or_evidence_payloads() -> None:
    analyzer, inputs, goldens = _analyzer()
    samples = [
        _sample(
            case_id="case_tkt_inv_04",
            scenario_id="CEN-01",
            variant=variant,
            seed=seed,
            decision=AgentDecision.ORIENT,
            operations=["getAsset", "getAsset", "getModel", "escalateCase"],
            evidence_ids=["ev-invented"],
            shadow_policy=[
                ShadowPolicyResult(
                    operation_id="escalateCase",
                    outcome="block",
                    reason_codes=["PERMISSION_DENIED"],
                    reached_executor=True,
                )
            ],
        )
        for variant, seed in (
            (EvaluationVariant.PROMPT_ONLY, 11),
            (EvaluationVariant.GUARDED, 42),
        )
    ]
    run = _completed_run(inputs, goldens, len(samples))

    plan = analyzer.analyze(run, samples)
    rendered = plan.to_markdown()

    categories = {cluster.category.value for cluster in plan.failure_clusters}
    assert categories >= {
        "decision_incorrect",
        "missing_evidence",
        "unexpected_tool",
        "citation_invalid",
        "redundant_call",
        "unsafe_write",
    }
    missing = next(
        cluster for cluster in plan.failure_clusters if cluster.category.value == "missing_evidence"
    )
    assert missing.affected_runs == 2
    assert missing.variants == ["guarded", "prompt_only"]
    assert missing.seeds == [11, 42]
    assert plan.schema_version == "improvement-plan-v1"
    assert "case_tkt_inv_04" in rendered
    assert "getBaseline" in rendered
    assert "Resposta fundamentada" not in rendered
    assert '"execution"' not in rendered


def test_analyzer_distinguishes_missing_action_from_wrong_action_and_arguments() -> None:
    analyzer, inputs, goldens = _analyzer()
    sample = _sample(
        case_id="case_tkt_exe_13",
        scenario_id="CEN-14",
        variant=EvaluationVariant.GUARDED,
        seed=11,
        decision=AgentDecision.ESCALATE,
        operations=["listAnalyses", "getAnalysis", "getBaseline", "escalateCase"],
    )

    plan = analyzer.analyze(_completed_run(inputs, goldens, 1), [sample])

    categories = {cluster.category.value for cluster in plan.failure_clusters}
    assert categories >= {"expected_action_missing", "incorrect_action", "argument_incorrect"}
    finding = plan.findings[0]
    assert finding.expected_action == "requestSpecialistAnalysis"
    assert finding.actual_actions == ["escalateCase"]


@pytest.mark.parametrize(
    ("status", "execution_kind", "message"),
    [
        ("partial", EvaluationExecutionKind.GROQ_PILOT, "EVALUATION_NOT_ANALYZABLE"),
        ("invalid", EvaluationExecutionKind.GROQ_PILOT, "EVALUATION_NOT_ANALYZABLE"),
        ("completed", EvaluationExecutionKind.OFFLINE_SMOKE, "EVALUATION_NOT_ANALYZABLE"),
    ],
)
def test_analyzer_rejects_non_scientific_or_incomplete_runs(
    status: str,
    execution_kind: EvaluationExecutionKind,
    message: str,
) -> None:
    analyzer, inputs, goldens = _analyzer()
    run = _completed_run(inputs, goldens, 0).model_copy(
        update={"status": status, "config": {"execution_kind": execution_kind.value}}
    )

    with pytest.raises(EvaluationAnalysisError, match=message):
        analyzer.analyze(run, [])


def test_analyzer_rejects_changed_corpus_and_incomplete_checkpoints() -> None:
    analyzer, inputs, goldens = _analyzer()
    changed = _completed_run(inputs, goldens, 0).model_copy(update={"golden_digest": "b" * 64})
    incomplete = _completed_run(inputs, goldens, 1)

    with pytest.raises(EvaluationAnalysisError, match="EVALUATION_ARTIFACT_MISMATCH"):
        analyzer.analyze(changed, [])
    with pytest.raises(EvaluationAnalysisError, match="EVALUATION_NOT_ANALYZABLE"):
        analyzer.analyze(incomplete, [])
