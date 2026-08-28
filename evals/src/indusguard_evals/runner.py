"""Coordenador com checkpoint por run e abertura tardia do golden set."""

from __future__ import annotations

from collections.abc import Mapping

from indusguard_api.agent import AgentTerminationReason
from indusguard_api.connectors import ConnectorCatalog

from indusguard_evals.contracts import (
    EvaluationExecutionKind,
    EvaluationPhase,
    EvaluationVariant,
)
from indusguard_evals.corpus import OfficialCorpus
from indusguard_evals.execution import VariantRuntime
from indusguard_evals.report import BenchmarkSummary, build_summary
from indusguard_evals.repository import EvaluationRepository
from indusguard_evals.schedule import FULL_SEEDS, PILOT_SEEDS, build_schedule, pending_schedule
from indusguard_evals.scorer import DeterministicScorer


class BenchmarkRunner:
    """Executa variantes pareadas; o scorer só nasce depois do último acesso ao modelo."""

    def __init__(
        self,
        *,
        corpus: OfficialCorpus,
        catalog: ConnectorCatalog,
        repository: EvaluationRepository,
        runtimes: Mapping[EvaluationVariant, VariantRuntime],
    ) -> None:
        if set(runtimes) != set(EvaluationVariant):
            raise ValueError("runner exige exatamente prompt_only e guarded")
        self._corpus = corpus
        self._catalog = catalog
        self._repository = repository
        self._runtimes = dict(runtimes)

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
                break

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
        )
        await self._repository.finish(
            evaluation_id,
            status="completed" if complete else "partial",
            summary=summary.model_dump(mode="json"),
            golden_digest=goldens.digest,
        )
        return summary
