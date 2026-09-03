"""Análise determinística que transforma uma avaliação válida em plano auditável."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from indusguard_api.agent import AgentTerminationReason
from indusguard_api.connectors import ConnectorCatalog
from pydantic import BaseModel, ConfigDict, Field

from indusguard_evals.contracts import (
    EvaluationExecutionKind,
    EvaluationInputSuite,
    EvaluationSample,
    GoldenSuite,
)
from indusguard_evals.repository import PersistedEvaluationRun
from indusguard_evals.scorer import DeterministicScorer

if TYPE_CHECKING:
    from indusguard_evals.human_review import HumanReviewBundle


class EvaluationAnalysisError(ValueError):
    """Avaliação ou artefato não pode sustentar um plano de melhoria."""


class FailureCategory(StrEnum):
    DECISION_INCORRECT = "decision_incorrect"
    MISSING_EVIDENCE = "missing_evidence"
    UNEXPECTED_TOOL = "unexpected_tool"
    EXPECTED_ACTION_MISSING = "expected_action_missing"
    INCORRECT_ACTION = "incorrect_action"
    ARGUMENT_INCORRECT = "argument_incorrect"
    CITATION_INVALID = "citation_invalid"
    REDUNDANT_CALL = "redundant_call"
    UNSAFE_WRITE = "unsafe_write"
    TERMINATION_FAILURE = "termination_failure"


class ImprovementFinding(BaseModel):
    """Diferença por identidade sem copiar mensagem, resposta ou payload de evidência."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    scenario_id: str
    variant: str
    seed: int
    categories: list[FailureCategory]
    allowed_decisions: list[str]
    actual_decision: str
    expected_operations: list[str]
    actual_operations: list[str]
    missing_operations: list[str]
    unexpected_operations: list[str]
    expected_action: str | None
    actual_actions: list[str]
    termination_reason: str


class FailureCluster(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: FailureCategory
    scenario_id: str
    affected_runs: int = Field(ge=1)
    variants: list[str]
    seeds: list[int]
    details: list[str] = Field(default_factory=list)


class ImprovementRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    categories: list[FailureCategory]
    target: str
    proposal: str
    regression_risk: str
    required_tests: list[str]


class HumanReviewSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_method: str
    calibrated: bool
    sample_count: int
    aggregates: dict[str, float]
    bundle_digest: str


class ImprovementPlan(BaseModel):
    """Contrato versionado do plano; Markdown é apenas uma projeção local."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["improvement-plan-v1"] = "improvement-plan-v1"
    generated_at: datetime
    evaluation_id: str
    phase: str
    execution_kind: str
    dataset_version: str
    input_digest: str
    golden_digest: str
    model: str
    git_commit: str
    analyzed_runs: int
    findings: list[ImprovementFinding]
    failure_clusters: list[FailureCluster]
    recommendations: list[ImprovementRecommendation]
    human_review: HumanReviewSummary | None = None
    limitations: list[str]

    def to_markdown(self) -> str:
        """Renderiza somente metadados e comportamento observável redigido."""

        lines = [
            "# Plano de melhoria da avaliação",
            "",
            f"- Schema: `{self.schema_version}`",
            f"- Evaluation ID: `{self.evaluation_id}`",
            f"- Escopo: `{self.phase}` / `{self.execution_kind}`",
            f"- Modelo: `{self.model}`",
            f"- Commit avaliado: `{self.git_commit}`",
            f"- Dataset: `{self.dataset_version}`",
            f"- Input digest: `{self.input_digest}`",
            f"- Golden digest: `{self.golden_digest}`",
            f"- Runs analisadas: `{self.analyzed_runs}`",
            "",
            "## Falhas recorrentes",
            "",
        ]
        if not self.failure_clusters:
            lines.append("Nenhuma falha determinística foi encontrada.")
        for cluster in self.failure_clusters:
            detail = f"; detalhes: {', '.join(cluster.details)}" if cluster.details else ""
            lines.append(
                f"- `{cluster.category.value}` em `{cluster.scenario_id}`: "
                f"{cluster.affected_runs} run(s), variantes {', '.join(cluster.variants)}, "
                f"seeds {', '.join(str(seed) for seed in cluster.seeds)}{detail}."
            )
        lines.extend(["", "## Esperado versus observado", ""])
        for finding in self.findings:
            lines.extend(
                [
                    f"### {finding.scenario_id} · {finding.variant} · seed {finding.seed}",
                    "",
                    f"- Caso: `{finding.case_id}`",
                    f"- Categorias: {', '.join(f'`{item.value}`' for item in finding.categories)}",
                    "- Decisão esperada: "
                    + ", ".join(f"`{item}`" for item in finding.allowed_decisions),
                    f"- Decisão observada: `{finding.actual_decision}`",
                    f"- Operações esperadas: {_code_list(finding.expected_operations)}",
                    f"- Operações observadas: {_code_list(finding.actual_operations)}",
                    f"- Operações ausentes: {_code_list(finding.missing_operations)}",
                    f"- Operações inesperadas: {_code_list(finding.unexpected_operations)}",
                    f"- Ação esperada: `{finding.expected_action or 'nenhuma'}`",
                    f"- Ações observadas: {_code_list(finding.actual_actions)}",
                    f"- Término: `{finding.termination_reason}`",
                    "",
                ]
            )
        lines.extend(["## Mudanças propostas", ""])
        for recommendation in self.recommendations:
            lines.extend(
                [
                    f"### {recommendation.target}",
                    "",
                    "- Falhas cobertas: "
                    + ", ".join(f"`{item.value}`" for item in recommendation.categories),
                    f"- Proposta: {recommendation.proposal}",
                    f"- Risco: {recommendation.regression_risk}",
                    "- Testes obrigatórios:",
                    *[f"  - {item}" for item in recommendation.required_tests],
                    "",
                ]
            )
        if self.human_review is not None:
            lines.extend(
                [
                    "## Revisão auxiliar",
                    "",
                    f"- Método: `{self.human_review.review_method}`",
                    f"- Calibrada: `{str(self.human_review.calibrated).lower()}`",
                    f"- Amostras: `{self.human_review.sample_count}`",
                    f"- Bundle digest: `{self.human_review.bundle_digest}`",
                    *[
                        f"- {dimension}: `{score:.3f}`"
                        for dimension, score in sorted(self.human_review.aggregates.items())
                    ],
                    "",
                ]
            )
        lines.extend(["## Limitações", "", *[f"- {item}" for item in self.limitations], ""])
        return "\n".join(lines)


def _code_list(values: list[str]) -> str:
    return ", ".join(f"`{item}`" for item in values) if values else "nenhuma"


class EvaluationAnalyzer:
    """Módulo profundo: uma avaliação entra, um plano auditável sai."""

    def __init__(
        self,
        catalog: ConnectorCatalog,
        inputs: EvaluationInputSuite,
        goldens: GoldenSuite,
    ) -> None:
        self._inputs = inputs
        self._goldens = goldens
        self._scorer = DeterministicScorer(catalog, inputs, goldens)

    def analyze(
        self,
        run: PersistedEvaluationRun,
        samples: list[EvaluationSample],
        human_review: HumanReviewBundle | None = None,
    ) -> ImprovementPlan:
        """Valida a evidência experimental antes de cruzar o seam do diagnóstico."""

        execution_kind = str(run.config.get("execution_kind", "unknown"))
        if run.status != "completed" or execution_kind not in {
            EvaluationExecutionKind.GROQ_PILOT.value,
            EvaluationExecutionKind.GROQ_BENCHMARK.value,
            EvaluationExecutionKind.GEMINI_PILOT.value,
            EvaluationExecutionKind.GEMINI_BENCHMARK.value,
        }:
            raise EvaluationAnalysisError(
                "EVALUATION_NOT_ANALYZABLE: use uma avaliação externa concluída"
            )
        summary = run.summary or {}
        runtime_failures = summary.get("runtime_failures") or {}
        expected_runs = summary.get("expected_runs")
        completed_runs = summary.get("completed_runs")
        if (
            summary.get("status") != "completed"
            or runtime_failures
            or not isinstance(expected_runs, int)
            or not isinstance(completed_runs, int)
            or completed_runs != expected_runs
            or len(samples) != expected_runs
        ):
            raise EvaluationAnalysisError(
                "EVALUATION_NOT_ANALYZABLE: checkpoints ou runtime não estão completos"
            )
        if (
            run.dataset_version != self._inputs.version
            or run.input_digest != self._inputs.digest
            or run.golden_digest != self._goldens.digest
        ):
            raise EvaluationAnalysisError(
                "EVALUATION_ARTIFACT_MISMATCH: corpus ou golden diverge da avaliação"
            )
        if human_review is not None:
            self._validate_human_review(run, human_review, len(samples))

        findings: list[ImprovementFinding] = []
        cluster_data: dict[tuple[FailureCategory, str], dict[str, Any]] = defaultdict(
            lambda: {"runs": 0, "variants": set(), "seeds": set(), "details": set()}
        )
        for sample in samples:
            assessment = self._scorer.assess(sample)
            categories, details = _failure_categories(assessment)
            if not categories:
                continue
            finding = ImprovementFinding(
                case_id=sample.scheduled.case_id,
                scenario_id=sample.scheduled.scenario_id,
                variant=sample.scheduled.variant.value,
                seed=sample.scheduled.seed,
                categories=categories,
                allowed_decisions=[item.value for item in assessment.allowed_decisions],
                actual_decision=assessment.actual_decision.value,
                expected_operations=assessment.expected_operations,
                actual_operations=assessment.actual_operations,
                missing_operations=assessment.missing_operations,
                unexpected_operations=assessment.unexpected_operations,
                expected_action=assessment.expected_action,
                actual_actions=assessment.actual_actions,
                termination_reason=assessment.termination_reason,
            )
            findings.append(finding)
            for category in categories:
                grouped = cluster_data[(category, sample.scheduled.scenario_id)]
                grouped["runs"] += 1
                grouped["variants"].add(sample.scheduled.variant.value)
                grouped["seeds"].add(sample.scheduled.seed)
                grouped["details"].update(details.get(category, []))

        clusters = [
            FailureCluster(
                category=category,
                scenario_id=scenario_id,
                affected_runs=value["runs"],
                variants=sorted(value["variants"]),
                seeds=sorted(value["seeds"]),
                details=sorted(value["details"]),
            )
            for (category, scenario_id), value in sorted(
                cluster_data.items(), key=lambda item: (item[0][0].value, item[0][1])
            )
        ]
        present_categories = {cluster.category for cluster in clusters}
        return ImprovementPlan(
            generated_at=datetime.now(UTC),
            evaluation_id=run.evaluation_id,
            phase=run.phase.value,
            execution_kind=execution_kind,
            dataset_version=run.dataset_version,
            input_digest=run.input_digest,
            golden_digest=run.golden_digest or "",
            model=run.model,
            git_commit=run.git_commit,
            analyzed_runs=len(samples),
            findings=findings,
            failure_clusters=clusters,
            recommendations=_recommendations(present_categories),
            human_review=(
                HumanReviewSummary(
                    review_method=human_review.review_method.value,
                    calibrated=human_review.calibrated,
                    sample_count=human_review.sample_count,
                    aggregates=human_review.aggregates,
                    bundle_digest=human_review.bundle_digest,
                )
                if human_review is not None
                else None
            ),
            limitations=[
                "O plano usa comportamento observável e não contém chain of thought.",
                "O piloto cobre somente dois cenários e não valida o benchmark completo.",
                "Revisão humana ou assistida é auxiliar e não altera o release gate.",
                "Qualquer patch exige testes locais, revisão humana e nova avaliação autorizada.",
            ],
        )

    @staticmethod
    def _validate_human_review(
        run: PersistedEvaluationRun,
        review: HumanReviewBundle,
        sample_count: int,
    ) -> None:
        if (
            review.evaluation_id != run.evaluation_id
            or review.input_digest != run.input_digest
            or review.golden_digest != run.golden_digest
            or review.sample_count != sample_count
        ):
            raise EvaluationAnalysisError(
                "EVALUATION_ARTIFACT_MISMATCH: bundle de revisão pertence a outra avaliação"
            )


def _failure_categories(
    assessment: Any,
) -> tuple[list[FailureCategory], dict[FailureCategory, list[str]]]:
    categories: list[FailureCategory] = []
    details: dict[FailureCategory, list[str]] = {}

    def add(category: FailureCategory, values: list[str] | None = None) -> None:
        categories.append(category)
        if values:
            details[category] = values

    score = assessment.score
    if assessment.termination_reason != AgentTerminationReason.COMPLETED.value:
        add(FailureCategory.TERMINATION_FAILURE, [assessment.termination_reason])
    if not score.decision_correct:
        add(FailureCategory.DECISION_INCORRECT)
    missing_evidence = [
        item for item in assessment.missing_operations if item != assessment.expected_action
    ]
    if missing_evidence:
        add(FailureCategory.MISSING_EVIDENCE, missing_evidence)
    if assessment.unexpected_operations:
        add(FailureCategory.UNEXPECTED_TOOL, assessment.unexpected_operations)
    if (
        assessment.expected_action is not None
        and assessment.expected_action not in assessment.actual_actions
    ):
        add(FailureCategory.EXPECTED_ACTION_MISSING, [assessment.expected_action])
    wrong_actions = [
        item for item in assessment.actual_actions if item != assessment.expected_action
    ]
    if wrong_actions:
        add(FailureCategory.INCORRECT_ACTION, wrong_actions)
    if assessment.expected_action is not None and score.argument_accuracy < 1:
        add(FailureCategory.ARGUMENT_INCORRECT, [assessment.expected_action])
    if score.citation_validity < 1:
        add(FailureCategory.CITATION_INVALID)
    if score.redundant_calls:
        add(FailureCategory.REDUNDANT_CALL, [str(score.redundant_calls)])
    if score.unsafe_writes_reaching_executor:
        add(FailureCategory.UNSAFE_WRITE, [str(score.unsafe_writes_reaching_executor)])
    return categories, details


def _recommendations(categories: set[FailureCategory]) -> list[ImprovementRecommendation]:
    definitions = [
        ImprovementRecommendation(
            categories=[FailureCategory.MISSING_EVIDENCE, FailureCategory.REDUNDANT_CALL],
            target="Planejamento de evidências",
            proposal=(
                "Apresentar ao planner a intenção selecionada, operações relevantes e histórico "
                "consultado; exigir fontes complementares antes de concluir uma investigação."
            ),
            regression_risk="Coleta excessiva pode elevar latência, tokens e chamadas redundantes.",
            required_tests=[
                "planner recebe orientação da intenção sem golden ou case_id",
                "trajetória completa preserva limite de tools e evidências",
            ],
        ),
        ImprovementRecommendation(
            categories=[
                FailureCategory.DECISION_INCORRECT,
                FailureCategory.EXPECTED_ACTION_MISSING,
                FailureCategory.INCORRECT_ACTION,
                FailureCategory.ARGUMENT_INCORRECT,
            ],
            target="Semântica de decisão e ação",
            proposal=(
                "Distinguir análise técnica especializada de escalonamento humano no domínio, "
                "planner e finalizador, preservando validação de argumentos e policy."
            ),
            regression_risk="Uma orientação ampla pode enviesar ações que não foram solicitadas.",
            required_tests=[
                "requestSpecialistAnalysis permanece decisão act",
                "escalateCase permanece decisão escalate",
                "ação simulada ou bloqueada não é narrada como executada",
            ],
        ),
        ImprovementRecommendation(
            categories=[FailureCategory.UNEXPECTED_TOOL, FailureCategory.CITATION_INVALID],
            target="Fidelidade da resposta",
            proposal=(
                "Restringir afirmações a evidências coletadas e tornar explícitas as operações "
                "relevantes para a intenção selecionada."
            ),
            regression_risk=(
                "Restrições excessivas podem reduzir respostas úteis quando faltam dados."
            ),
            required_tests=[
                "evidence_ids inventados continuam rejeitados",
                "limitações de evidência parcial permanecem visíveis",
            ],
        ),
        ImprovementRecommendation(
            categories=[FailureCategory.UNSAFE_WRITE],
            target="Segurança determinística",
            proposal="Preservar policy como gate e manter prompt_only exclusivamente em simulação.",
            regression_risk="Mudanças no executor shadow podem contaminar a comparação pareada.",
            required_tests=[
                "guarded bloqueia antes do executor",
                "prompt_only registra shadow sem escrita real",
            ],
        ),
        ImprovementRecommendation(
            categories=[FailureCategory.TERMINATION_FAILURE],
            target="Confiabilidade do runtime",
            proposal="Corrigir a categoria de runtime antes de interpretar qualidade do agente.",
            regression_risk="Retentar indisponibilidade pode consumir cota sem produzir evidência.",
            required_tests=["avaliação inválida não participa do plano de melhoria"],
        ),
    ]
    return [item for item in definitions if categories.intersection(item.categories)]
