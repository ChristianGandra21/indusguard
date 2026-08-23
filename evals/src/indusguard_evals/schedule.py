"""Agenda pareada do benchmark com ordem contrabalanceada e identidade retomável."""

from collections.abc import Iterable

from indusguard_evals.contracts import (
    EvaluationInputSuite,
    EvaluationPhase,
    EvaluationVariant,
    ScheduledRun,
)

PILOT_SEEDS = (11, 42, 73)
FULL_SEEDS = (42,)


def build_schedule(
    inputs: EvaluationInputSuite,
    phase: EvaluationPhase,
) -> list[ScheduledRun]:
    """Cria pares adjacentes alternando qual variante executa primeiro.

    O contrabalanceamento não elimina efeitos temporais, mas evita favorecer sempre a primeira
    variante quando há cold start, aquecimento da fixture ou variação de cota do provedor.
    """

    cases = inputs.pilot_cases if phase is EvaluationPhase.PILOT else inputs.cases
    seeds = PILOT_SEEDS if phase is EvaluationPhase.PILOT else FULL_SEEDS
    scheduled: list[ScheduledRun] = []
    pair_index = 0
    for case in cases:
        for seed in seeds:
            variants = (
                (EvaluationVariant.PROMPT_ONLY, EvaluationVariant.GUARDED)
                if pair_index % 2 == 0
                else (EvaluationVariant.GUARDED, EvaluationVariant.PROMPT_ONLY)
            )
            for variant in variants:
                scheduled.append(
                    ScheduledRun(
                        case_id=case.case_id,
                        scenario_id=case.scenario_id,
                        variant=variant,
                        seed=seed,
                        ordinal=len(scheduled),
                    )
                )
            pair_index += 1
    return scheduled


def pending_schedule(
    schedule: Iterable[ScheduledRun],
    completed: set[tuple[str, EvaluationVariant, int]],
) -> list[ScheduledRun]:
    """Remove checkpoints concluídos usando a identidade experimental, não a posição."""

    return [item for item in schedule if item.identity not in completed]
