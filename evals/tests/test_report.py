"""A conclusão da hipótese é conservadora e mantém as ressalvas pré-registradas."""

from datetime import UTC, datetime

import pytest

from indusguard_evals.contracts import CaseScore, EvaluationVariant
from indusguard_evals.report import BenchmarkInterruption, build_summary


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
