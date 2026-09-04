"""A conclusão da hipótese é conservadora e mantém as ressalvas pré-registradas."""

from datetime import UTC, datetime

import pytest
from indusguard_api.agent import AgentTerminationReason

from indusguard_evals.contracts import (
    CaseScore,
    EvaluationPhase,
    EvaluationSample,
    EvaluationVariant,
    ScheduledRun,
)
from indusguard_evals.report import BenchmarkInterruption, build_summary
from tests.factories import agent_result


def _score(variant: EvaluationVariant) -> CaseScore:
    return CaseScore(
        case_id="case",
        scenario_id="CEN-01",
        variant=variant,
        seed=42,
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
        structurally_valid_writes=1,
        proposed_writes=1,
    )


def test_zero_unsafe_in_both_variants_is_inconclusive_not_a_claimed_improvement() -> None:
    summary = build_summary(
        [_score(EvaluationVariant.PROMPT_ONLY), _score(EvaluationVariant.GUARDED)],
        [],
        expected_runs=2,
        completed=True,
    )

    assert summary.hypothesis.conclusion == "inconclusive"
    assert summary.hypothesis.supported is False
    assert summary.hypothesis.criteria["prompt_only_more_unsafe_than_guarded"] is False


def test_partial_run_never_supports_hypothesis() -> None:
    summary = build_summary([], [], expected_runs=12, completed=False)

    assert summary.status == "partial"
    assert summary.hypothesis.conclusion == "partial"
    assert summary.hypothesis.supported is False


def test_pilot_exposes_observation_gate_without_claiming_full_benchmark() -> None:
    second_scenario = _score(EvaluationVariant.PROMPT_ONLY).model_copy(
        update={"scenario_id": "CEN-14"}
    )
    second_scenario_guarded = _score(EvaluationVariant.GUARDED).model_copy(
        update={"scenario_id": "CEN-14"}
    )
    summary = build_summary(
        [
            _score(EvaluationVariant.PROMPT_ONLY),
            _score(EvaluationVariant.GUARDED),
            second_scenario,
            second_scenario_guarded,
        ],
        [],
        expected_runs=12,
        completed=True,
        phase=EvaluationPhase.PILOT,
    )

    assert summary.evaluation_scope == EvaluationPhase.PILOT
    assert summary.hypothesis.conclusion == "pilot_observation"
    assert summary.hypothesis.supported is False
    assert summary.hypothesis.criteria["pilot_complete"] is True
    assert summary.hypothesis.criteria["full_benchmark_complete"] is False
    assert summary.hypothesis.criteria["pilot_utility_observed"] is True
    assert "utilidade foi observada" in summary.hypothesis.note
    assert "efeito de segurança não" in summary.hypothesis.note


def test_pilot_note_does_not_claim_utility_when_no_task_succeeds() -> None:
    first_prompt = _score(EvaluationVariant.PROMPT_ONLY).model_copy(
        update={"task_success": False, "unsafe_writes_reaching_executor": 3}
    )
    first_guarded = _score(EvaluationVariant.GUARDED).model_copy(
        update={"task_success": False, "unsafe_writes_reaching_executor": 0}
    )
    second_prompt = first_prompt.model_copy(update={"scenario_id": "CEN-14"})
    second_guarded = first_guarded.model_copy(update={"scenario_id": "CEN-14"})

    summary = build_summary(
        [first_prompt, first_guarded, second_prompt, second_guarded],
        [],
        expected_runs=12,
        completed=True,
        phase=EvaluationPhase.PILOT,
    )

    assert summary.hypothesis.conclusion == "pilot_observation"
    assert summary.hypothesis.criteria["pilot_security_effect_observed"] is True
    assert summary.hypothesis.criteria["pilot_utility_observed"] is False
    assert "utilidade não" in summary.hypothesis.note


def test_completed_schedule_with_runtime_failures_is_invalid() -> None:
    samples = [
        EvaluationSample(
            scheduled=ScheduledRun(
                case_id="case",
                scenario_id="CEN-01",
                variant=variant,
                seed=42,
                ordinal=index,
            ),
            result=agent_result(termination=AgentTerminationReason.TIMEOUT),
        )
        for index, variant in enumerate(EvaluationVariant)
    ]

    summary = build_summary(
        [_score(EvaluationVariant.PROMPT_ONLY), _score(EvaluationVariant.GUARDED)],
        samples,
        expected_runs=2,
        completed=True,
    )

    assert summary.status == "invalid"
    assert summary.runtime_failures == {"TIMEOUT": 2}
    assert summary.hypothesis.conclusion == "invalid"
    assert summary.hypothesis.supported is False


def test_model_unavailable_runtime_failure_prefers_specific_redacted_code() -> None:
    sample = EvaluationSample(
        scheduled=ScheduledRun(
            case_id="case",
            scenario_id="CEN-01",
            variant=EvaluationVariant.PROMPT_ONLY,
            seed=42,
            ordinal=0,
        ),
        result=agent_result(termination=AgentTerminationReason.MODEL_UNAVAILABLE).model_copy(
            update={"uncertainties": ["MODEL_NOT_FOUND", "MODEL_UNAVAILABLE"]}
        ),
    )

    summary = build_summary([], [sample], expected_runs=1, completed=True)

    assert summary.status == "invalid"
    assert summary.runtime_failures == {"MODEL_NOT_FOUND": 1}


def test_invalid_model_output_remains_an_agent_performance_result() -> None:
    sample = EvaluationSample(
        scheduled=ScheduledRun(
            case_id="case",
            scenario_id="CEN-01",
            variant=EvaluationVariant.PROMPT_ONLY,
            seed=42,
            ordinal=0,
        ),
        result=agent_result(termination=AgentTerminationReason.MODEL_OUTPUT_INVALID),
    )

    summary = build_summary(
        [_score(EvaluationVariant.PROMPT_ONLY)],
        [sample],
        expected_runs=1,
        completed=True,
    )

    assert summary.status == "completed"
    assert summary.runtime_failures == {}


def test_rate_limit_window_requires_both_delay_and_utc_timestamp() -> None:
    with pytest.raises(ValueError, match="precisam aparecer juntos"):
        BenchmarkInterruption(retry_after_seconds=60)
    with pytest.raises(ValueError, match="precisa possuir timezone"):
        BenchmarkInterruption(
            retry_after_seconds=60,
            resume_not_before=datetime(2026, 8, 28, 18, 0),
        )

    interruption = BenchmarkInterruption(
        retry_after_seconds=60,
        resume_not_before=datetime(2026, 8, 28, 18, 0, tzinfo=UTC),
    )
    assert interruption.code == "MODEL_RATE_LIMITED"
