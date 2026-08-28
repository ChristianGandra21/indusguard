"""Coordenador com checkpoint por run e abertura tardia do golden set."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Literal

from indusguard_api.agent import AgentTerminationReason
from indusguard_api.connectors import ConnectorCatalog
from pydantic import BaseModel, ConfigDict, Field

from indusguard_evals.contracts import (
    EvaluationExecutionKind,
    EvaluationPhase,
    EvaluationVariant,
)
from indusguard_evals.corpus import OfficialCorpus
from indusguard_evals.execution import VariantRuntime
from indusguard_evals.report import BenchmarkInterruption, BenchmarkSummary, build_summary
from indusguard_evals.repository import EvaluationRepository
from indusguard_evals.schedule import FULL_SEEDS, PILOT_SEEDS, build_schedule, pending_schedule
from indusguard_evals.scorer import DeterministicScorer


class EvaluationProgress(BaseModel):
    """Evento seguro para feedback incremental do CLI."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: Literal["evaluation_progress"] = "evaluation_progress"
    evaluation_id: str
    completed_runs: int = Field(ge=0)
    expected_runs: int = Field(ge=0)
    checkpoint_status: Literal["completed", "rate_limited"]
    case_id: str
    scenario_id: str
    variant: EvaluationVariant
    seed: int


class BenchmarkRunner:
    """Executa variantes pareadas; o scorer só nasce depois do último acesso ao modelo."""

    def __init__(
        self,
        *,
        corpus: OfficialCorpus,
        catalog: ConnectorCatalog,
        repository: EvaluationRepository,
        runtimes: Mapping[EvaluationVariant, VariantRuntime],
        on_progress: Callable[[EvaluationProgress], None] | None = None,
    ) -> None:
        if set(runtimes) != set(EvaluationVariant):
            raise ValueError("runner exige exatamente prompt_only e guarded")
        self._corpus = corpus
        self._catalog = catalog
        self._repository = repository
        self._runtimes = dict(runtimes)
        self._on_progress = on_progress

    def _emit_progress(self, progress: EvaluationProgress) -> None:
        if self._on_progress is None:
            return
        try:
            self._on_progress(progress)
        except Exception:
            # Feedback de terminal nunca pode interromper ou invalidar uma avaliação.
            return

    async def start(
        self,
        *,
        phase: EvaluationPhase,
        model: str,
        git_commit: str,
        execution_kind: EvaluationExecutionKind = EvaluationExecutionKind.UNKNOWN,
        preflight_manifest_digest: str | None = None,
    ) -> str:
        inputs = self._corpus.load_inputs()
        seeds = PILOT_SEEDS if phase is EvaluationPhase.PILOT else FULL_SEEDS
        config = {
            "seeds": list(seeds),
            "variants": [item.value for item in EvaluationVariant],
            "counterbalanced": True,
            "execution_kind": execution_kind.value,
        }
        if preflight_manifest_digest is not None:
            config["preflight_manifest_digest"] = preflight_manifest_digest
        return await self._repository.start(
            phase=phase,
            dataset_version=inputs.version,
            input_digest=inputs.digest,
            model=model,
            git_commit=git_commit,
            config=config,
        )

    async def execute(self, evaluation_id: str) -> BenchmarkSummary:
        run = await self._repository.get(evaluation_id)
        if run is None:
            raise KeyError(f"avaliação não encontrada: {evaluation_id}")
        inputs = self._corpus.load_inputs()
        if run.dataset_version != inputs.version or run.input_digest != inputs.digest:
            raise ValueError("o corpus mudou desde a criação desta avaliação")
        schedule = build_schedule(inputs, run.phase)
        completed_identities = await self._repository.completed_identities(evaluation_id)
        case_by_id = {case.case_id: case for case in inputs.cases}
        rate_limited = False
        interruption: BenchmarkInterruption | None = None
        completed_runs = len(completed_identities)
        for scheduled in pending_schedule(schedule, completed_identities):
            sample = await self._runtimes[scheduled.variant].run(
                scheduled,
                case_by_id[scheduled.case_id],
            )
            await self._repository.checkpoint(evaluation_id, sample)
            if (
                sample.result.metrics.termination_reason
                is AgentTerminationReason.MODEL_RATE_LIMITED
            ):
                rate_limited = True
                retry_after = sample.result.metrics.retry_after_seconds
                interrupted_at = datetime.now(UTC)
                interruption = BenchmarkInterruption(
                    retry_after_seconds=retry_after,
                    resume_not_before=(
                        interrupted_at + timedelta(seconds=retry_after)
                        if retry_after is not None
                        else None
                    ),
                )
                self._emit_progress(
                    EvaluationProgress(
                        evaluation_id=evaluation_id,
                        completed_runs=completed_runs,
                        expected_runs=len(schedule),
                        checkpoint_status="rate_limited",
                        case_id=scheduled.case_id,
                        scenario_id=scheduled.scenario_id,
                        variant=scheduled.variant,
                        seed=scheduled.seed,
                    )
                )
                break
            completed_runs += 1
            self._emit_progress(
                EvaluationProgress(
                    evaluation_id=evaluation_id,
                    completed_runs=completed_runs,
                    expected_runs=len(schedule),
                    checkpoint_status="completed",
                    case_id=scheduled.case_id,
                    scenario_id=scheduled.scenario_id,
                    variant=scheduled.variant,
                    seed=scheduled.seed,
                )
            )

        # Esta é a única fronteira que abre goldens, deliberadamente depois do loop de runs.
        samples = await self._repository.samples(evaluation_id)
        goldens = self._corpus.load_goldens(inputs)
        scorer = DeterministicScorer(self._catalog, inputs, goldens)
        scores = [scorer.score(sample) for sample in samples]
        for score in scores:
            await self._repository.apply_score(evaluation_id, score)
        complete = not rate_limited and len(samples) == len(schedule)
        summary = build_summary(
            scores,
            samples,
            expected_runs=len(schedule),
            completed=complete,
            interruption=interruption,
        )
        await self._repository.finish(
            evaluation_id,
            status="completed" if complete else "partial",
            summary=summary.model_dump(mode="json"),
            golden_digest=goldens.digest,
        )
        return summary
