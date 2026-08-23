"""Agendamento reproduzível, pareado e retomável do benchmark."""

from pathlib import Path

from indusguard_evals.contracts import EvaluationPhase, EvaluationVariant
from indusguard_evals.corpus import OfficialCorpus
from indusguard_evals.schedule import build_schedule, pending_schedule

CORPUS_ROOT = Path(__file__).resolve().parents[1] / "corpus" / "official-v1"


def test_pilot_and_full_schedules_are_paired_and_counterbalanced() -> None:
    inputs = OfficialCorpus(CORPUS_ROOT).load_inputs()

    pilot = build_schedule(inputs, EvaluationPhase.PILOT)
    full = build_schedule(inputs, EvaluationPhase.FULL)

    assert len(pilot) == 12
    assert len(full) == 34
    assert {item.seed for item in pilot} == {11, 42, 73}
    assert {item.seed for item in full} == {42}

    for schedule in (pilot, full):
        pairs: dict[tuple[str, int], list[EvaluationVariant]] = {}
        for item in schedule:
            pairs.setdefault((item.case_id, item.seed), []).append(item.variant)
        assert all(set(variants) == set(EvaluationVariant) for variants in pairs.values())
        first_variants = [variants[0] for variants in pairs.values()]
        assert EvaluationVariant.PROMPT_ONLY in first_variants
        assert EvaluationVariant.GUARDED in first_variants
        assert [item.ordinal for item in schedule] == list(range(len(schedule)))


def test_resume_removes_completed_identity_without_duplicating_runs() -> None:
    inputs = OfficialCorpus(CORPUS_ROOT).load_inputs()
    schedule = build_schedule(inputs, EvaluationPhase.PILOT)
    completed = {schedule[0].identity, schedule[3].identity}

    pending = pending_schedule(schedule, completed)

    assert len(pending) == len(schedule) - 2
    assert completed.isdisjoint(item.identity for item in pending)
