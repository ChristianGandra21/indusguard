"""Compara duas avaliações elegíveis sem expor payloads ou goldens."""

from __future__ import annotations

from datetime import UTC, datetime
from statistics import median
from typing import Literal

from indusguard_api.connectors import ConnectorCatalog
from pydantic import BaseModel, ConfigDict

from indusguard_evals.analysis import EvaluationAnalyzer, FailureCategory, ImprovementPlan
from indusguard_evals.contracts import (
    CaseScore,
    EvaluationExecutionKind,
    EvaluationInputSuite,
    EvaluationSample,
    GoldenSuite,
)
from indusguard_evals.repository import PersistedEvaluationRun
from indusguard_evals.scorer import DeterministicScorer


class EvaluationComparisonError(ValueError):
    """As avaliações não formam uma comparação determinística válida."""


IdentityOutcome = Literal["improved", "regressed", "mixed", "unchanged"]
FailureStatus = Literal["resolved", "persistent", "new"]


class MetricDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseline: float
    candidate: float
    delta: float


class RuntimeComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    median_latency_ms: MetricDelta


class SafetyComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    safe_success_rate: MetricDelta
    unsafe_writes_reaching_executor: MetricDelta
    scope_security_success_rate: MetricDelta


class UtilityComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_success_rate: MetricDelta
    decision_correct_rate: MetricDelta
    evidence_coverage: MetricDelta
    tool_precision: MetricDelta
    tool_recall: MetricDelta
    argument_accuracy: MetricDelta
    citation_validity: MetricDelta


class IdentityComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    scenario_id: str
    variant: str
    seed: int
    outcome: IdentityOutcome


class FailureChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: FailureCategory
    scenario_id: str
    baseline_count: int
    candidate_count: int
    status: FailureStatus


class EvaluationComparison(BaseModel):
    """Contrato redigido e versionado da comparação."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["evaluation-comparison-v1"] = "evaluation-comparison-v1"
    generated_at: datetime
    baseline_evaluation_id: str
    candidate_evaluation_id: str
    phase: str
    execution_kind: str
    dataset_version: str
    input_digest: str
    golden_digest: str
    baseline_model: str
    candidate_model: str
    baseline_git_commit: str
    candidate_git_commit: str
    compared_runs: int
    runtime: RuntimeComparison
    safety: SafetyComparison
    utility: UtilityComparison
    identities: list[IdentityComparison]
    failure_changes: list[FailureChange]
    limitations: list[str]

    def to_markdown(self) -> str:
        """Projeta apenas metadados, métricas e classificações redigidas."""

        lines = [
            "# Comparação de avaliações",
            "",
            f"- Schema: `{self.schema_version}`",
            f"- Baseline: `{self.baseline_evaluation_id}` (`{self.baseline_git_commit}`)",
            f"- Candidata: `{self.candidate_evaluation_id}` (`{self.candidate_git_commit}`)",
            f"- Escopo: `{self.phase}` / `{self.execution_kind}`",
            f"- Modelo: `{self.baseline_model}` → `{self.candidate_model}`",
            f"- Dataset: `{self.dataset_version}`",
            f"- Input digest: `{self.input_digest}`",
            f"- Golden digest: `{self.golden_digest}`",
            f"- Identidades comparadas: `{self.compared_runs}`",
            "",
            "## Runtime",
            "",
            _metric_line("Latência mediana (ms)", self.runtime.median_latency_ms),
            "",
            "## Segurança",
            "",
            _metric_line("Taxa de sucesso seguro", self.safety.safe_success_rate),
            _metric_line(
                "Escritas inseguras no executor",
                self.safety.unsafe_writes_reaching_executor,
            ),
            _metric_line(
                "Taxa de segurança de escopo",
                self.safety.scope_security_success_rate,
            ),
            "",
            "## Utilidade",
            "",
            _metric_line("Taxa de sucesso da tarefa", self.utility.task_success_rate),
            _metric_line("Decisão correta", self.utility.decision_correct_rate),
            _metric_line("Cobertura de evidência", self.utility.evidence_coverage),
            _metric_line("Precisão de tools", self.utility.tool_precision),
            _metric_line("Recall de tools", self.utility.tool_recall),
            _metric_line("Precisão de argumentos", self.utility.argument_accuracy),
            _metric_line("Validade de citações", self.utility.citation_validity),
            "",
            "## Resultado por identidade",
            "",
        ]
        lines.extend(
            f"- `{item.scenario_id}` / `{item.variant}` / seed `{item.seed}`: `{item.outcome}`."
            for item in self.identities
        )
        lines.extend(["", "## Mudanças nas falhas", ""])
        if not self.failure_changes:
            lines.append("Nenhuma falha determinística nas duas avaliações.")
        lines.extend(
            f"- `{item.category.value}` em `{item.scenario_id}`: `{item.status}` "
            f"({item.baseline_count} → {item.candidate_count})."
            for item in self.failure_changes
        )
        lines.extend(["", "## Limitações", "", *[f"- {item}" for item in self.limitations], ""])
        return "\n".join(lines)


def _metric_line(label: str, metric: MetricDelta) -> str:
    return (
        f"- {label}: `{metric.baseline:.3f}` → `{metric.candidate:.3f}` "
        f"(delta `{metric.delta:+.3f}`)."
    )


class EvaluationComparator:
    """Módulo profundo: duas avaliações compatíveis entram, uma comparação sai."""

    def __init__(
        self,
        catalog: ConnectorCatalog,
        inputs: EvaluationInputSuite,
        goldens: GoldenSuite,
    ) -> None:
        self._inputs = inputs
        self._goldens = goldens
        self._scorer = DeterministicScorer(catalog, inputs, goldens)
        self._analyzer = EvaluationAnalyzer(catalog, inputs, goldens)

    def compare(
        self,
        baseline: PersistedEvaluationRun,
        baseline_samples: list[EvaluationSample],
        candidate: PersistedEvaluationRun,
        candidate_samples: list[EvaluationSample],
    ) -> EvaluationComparison:
        """Compara scores pareados e usa o analisador para classificar falhas."""

        if baseline.evaluation_id == candidate.evaluation_id:
            raise EvaluationComparisonError(
                "EVALUATION_COMPARISON_INVALID: baseline e candidata devem ser distintas"
            )
        self._validate_eligibility(baseline, baseline_samples)
        self._validate_eligibility(candidate, candidate_samples)
        if (
            baseline.phase != candidate.phase
            or baseline.dataset_version != candidate.dataset_version
            or baseline.input_digest != candidate.input_digest
            or baseline.golden_digest != candidate.golden_digest
            or baseline.model != candidate.model
            or baseline.config.get("execution_kind") != candidate.config.get("execution_kind")
        ):
            raise EvaluationComparisonError(
                "EVALUATION_COMPARISON_MISMATCH: metadados experimentais divergem"
            )
        baseline_identities = [_identity(sample) for sample in baseline_samples]
        candidate_identities = [_identity(sample) for sample in candidate_samples]
        if (
            len(baseline_identities) != len(set(baseline_identities))
            or len(candidate_identities) != len(set(candidate_identities))
            or set(baseline_identities) != set(candidate_identities)
        ):
            raise EvaluationComparisonError(
                "EVALUATION_COMPARISON_MISMATCH: agendas experimentais divergem"
            )

        baseline_plan = self._analyzer.analyze(baseline, baseline_samples)
        candidate_plan = self._analyzer.analyze(candidate, candidate_samples)
        baseline_scores = [self._scorer.score(sample) for sample in baseline_samples]
        candidate_scores = [self._scorer.score(sample) for sample in candidate_samples]

        baseline_by_identity = {
            _identity(sample): score
            for sample, score in zip(baseline_samples, baseline_scores, strict=True)
        }
        candidate_by_identity = {
            _identity(sample): score
            for sample, score in zip(candidate_samples, candidate_scores, strict=True)
        }
        identities = [
            IdentityComparison(
                case_id=identity[0],
                scenario_id=identity[1],
                variant=identity[2],
                seed=identity[3],
                outcome=_outcome(baseline_by_identity[identity], candidate_by_identity[identity]),
            )
            for identity in sorted(baseline_by_identity)
        ]

        return EvaluationComparison(
            generated_at=datetime.now(UTC),
            baseline_evaluation_id=baseline.evaluation_id,
            candidate_evaluation_id=candidate.evaluation_id,
            phase=baseline.phase.value,
            execution_kind=str(baseline.config.get("execution_kind", "unknown")),
            dataset_version=baseline.dataset_version,
            input_digest=baseline.input_digest,
            golden_digest=baseline.golden_digest or "",
            baseline_model=baseline.model,
            candidate_model=candidate.model,
            baseline_git_commit=baseline.git_commit,
            candidate_git_commit=candidate.git_commit,
            compared_runs=len(identities),
            runtime=RuntimeComparison(
                median_latency_ms=_delta(
                    median(sample.result.metrics.latency_ms for sample in baseline_samples),
                    median(sample.result.metrics.latency_ms for sample in candidate_samples),
                )
            ),
            safety=SafetyComparison(
                safe_success_rate=_score_delta(baseline_scores, candidate_scores, "safe_success"),
                unsafe_writes_reaching_executor=_score_delta(
                    baseline_scores, candidate_scores, "unsafe_writes_reaching_executor", mean=False
                ),
                scope_security_success_rate=_scope_security_delta(
                    baseline_scores, candidate_scores
                ),
            ),
            utility=UtilityComparison(
                task_success_rate=_score_delta(baseline_scores, candidate_scores, "task_success"),
                decision_correct_rate=_score_delta(
                    baseline_scores, candidate_scores, "decision_correct"
                ),
                evidence_coverage=_score_delta(
                    baseline_scores, candidate_scores, "evidence_coverage"
                ),
                tool_precision=_score_delta(baseline_scores, candidate_scores, "tool_precision"),
                tool_recall=_score_delta(baseline_scores, candidate_scores, "tool_recall"),
                argument_accuracy=_score_delta(
                    baseline_scores, candidate_scores, "argument_accuracy"
                ),
                citation_validity=_score_delta(
                    baseline_scores, candidate_scores, "citation_validity"
                ),
            ),
            identities=identities,
            failure_changes=_failure_changes(baseline_plan, candidate_plan),
            limitations=[
                "A comparação é observacional e não demonstra causalidade.",
                "Um piloto cobre somente dois cenários e não substitui o benchmark completo.",
                "A comparação não usa revisão humana ou assistida como release gate.",
                "Ganho real de qualidade exige uma nova execução autorizada e compatível.",
            ],
        )

    def _validate_eligibility(
        self,
        run: PersistedEvaluationRun,
        samples: list[EvaluationSample],
    ) -> None:
        execution_kind = str(run.config.get("execution_kind", "unknown"))
        if run.status != "completed" or execution_kind not in {
            EvaluationExecutionKind.GROQ_PILOT.value,
            EvaluationExecutionKind.GROQ_BENCHMARK.value,
        }:
            raise EvaluationComparisonError(
                "EVALUATION_NOT_COMPARABLE: use uma avaliação Groq concluída"
            )
        summary = run.summary or {}
        expected_runs = summary.get("expected_runs")
        completed_runs = summary.get("completed_runs")
        if (
            summary.get("status") != "completed"
            or summary.get("runtime_failures")
            or not isinstance(expected_runs, int)
            or not isinstance(completed_runs, int)
            or completed_runs != expected_runs
            or len(samples) != expected_runs
        ):
            raise EvaluationComparisonError(
                "EVALUATION_NOT_COMPARABLE: checkpoints ou runtime não estão completos"
            )
        if (
            run.dataset_version != self._inputs.version
            or run.input_digest != self._inputs.digest
            or run.golden_digest != self._goldens.digest
        ):
            raise EvaluationComparisonError(
                "EVALUATION_ARTIFACT_MISMATCH: corpus ou golden diverge da avaliação"
            )


def _identity(sample: EvaluationSample) -> tuple[str, str, str, int]:
    scheduled = sample.scheduled
    return (
        scheduled.case_id,
        scheduled.scenario_id,
        scheduled.variant.value,
        scheduled.seed,
    )


def _delta(baseline: float, candidate: float) -> MetricDelta:
    baseline_value = float(baseline)
    candidate_value = float(candidate)
    return MetricDelta(
        baseline=baseline_value,
        candidate=candidate_value,
        delta=candidate_value - baseline_value,
    )


def _score_delta(
    baseline: list[CaseScore],
    candidate: list[CaseScore],
    field: str,
    *,
    mean: bool = True,
) -> MetricDelta:
    divisor = len(baseline) if mean else 1
    candidate_divisor = len(candidate) if mean else 1
    return _delta(
        sum(float(getattr(score, field)) for score in baseline) / divisor,
        sum(float(getattr(score, field)) for score in candidate) / candidate_divisor,
    )


def _scope_security_delta(baseline: list[CaseScore], candidate: list[CaseScore]) -> MetricDelta:
    def rate(scores: list[CaseScore]) -> float:
        eligible = [
            score.scope_security_success for score in scores if score.scope_security_eligible
        ]
        return sum(float(value) for value in eligible) / len(eligible) if eligible else 1.0

    return _delta(rate(baseline), rate(candidate))


def _quality_vector(score: CaseScore) -> tuple[float, ...]:
    return (
        float(score.task_success),
        float(score.safe_success),
        float(score.decision_correct),
        score.evidence_coverage,
        score.tool_precision,
        score.tool_recall,
        score.argument_accuracy,
        score.citation_validity,
        -float(score.redundant_calls),
        -float(score.unsafe_writes_reaching_executor),
    )


def _outcome(baseline: CaseScore, candidate: CaseScore) -> IdentityOutcome:
    baseline_vector = _quality_vector(baseline)
    candidate_vector = _quality_vector(candidate)
    pairs = zip(baseline_vector, candidate_vector, strict=True)
    differences = [right - left for left, right in pairs]
    better = any(difference > 0 for difference in differences)
    worse = any(difference < 0 for difference in differences)
    if better and not worse:
        return "improved"
    if worse and not better:
        return "regressed"
    if better and worse:
        return "mixed"
    return "unchanged"


def _failure_changes(
    baseline_plan: ImprovementPlan,
    candidate_plan: ImprovementPlan,
) -> list[FailureChange]:
    baseline_clusters = {
        (cluster.category, cluster.scenario_id): cluster.affected_runs
        for cluster in baseline_plan.failure_clusters
    }
    candidate_clusters = {
        (cluster.category, cluster.scenario_id): cluster.affected_runs
        for cluster in candidate_plan.failure_clusters
    }
    changes: list[FailureChange] = []
    for category, scenario_id in sorted(
        baseline_clusters.keys() | candidate_clusters.keys(),
        key=lambda item: (item[0].value, item[1]),
    ):
        baseline_count = baseline_clusters.get((category, scenario_id), 0)
        candidate_count = candidate_clusters.get((category, scenario_id), 0)
        status: FailureStatus = "persistent"
        if baseline_count and not candidate_count:
            status = "resolved"
        elif candidate_count and not baseline_count:
            status = "new"
        changes.append(
            FailureChange(
                category=category,
                scenario_id=scenario_id,
                baseline_count=baseline_count,
                candidate_count=candidate_count,
                status=status,
            )
        )
    return changes
