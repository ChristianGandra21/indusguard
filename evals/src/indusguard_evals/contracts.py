"""Contratos versionados do benchmark, separados dos contratos do agente em produção."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from indusguard_api.agent import AgentDecision, AgentRunResult
from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvaluationVariant(StrEnum):
    """Única diferença experimental permitida entre as runs pareadas."""

    PROMPT_ONLY = "prompt_only"
    GUARDED = "guarded"


class EvaluationPhase(StrEnum):
    PILOT = "pilot"
    FULL = "full"


class EvaluationExecutionKind(StrEnum):
    """Distingue prova científica de um smoke que usa modelo fake."""

    OFFLINE_SMOKE = "offline_smoke"
    GROQ_PILOT = "groq_pilot"
    GROQ_BENCHMARK = "groq_benchmark"
    GEMINI_PILOT = "gemini_pilot"
    GEMINI_BENCHMARK = "gemini_benchmark"
    UNKNOWN = "unknown"


class StakeholderCase(BaseModel):
    """Entrada original entregue ao agente, sem trajetória ou resposta esperada."""

    model_config = ConfigDict(extra="forbid")

    id: str
    ticket_id: str
    company_id: str
    user_id: str
    asset_id: str
    message: str


class RunContextEntry(BaseModel):
    """Sinais declarados pelo host antes da run, sem informação de avaliação."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    scenario_id: str = Field(pattern=r"^CEN-[0-9]{2}$")
    direct_request: bool


class EvaluationCaseInput(BaseModel):
    """Caso pronto para execução, ainda completamente independente do golden set."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    ticket_id: str
    scenario_id: str
    connector_id: str
    company_id: str
    user_id: str
    asset_id: str
    message: str
    direct_request: bool


class EvaluationInputSuite(BaseModel):
    """Visão que pode existir no processo do agente sem contaminar seu planejamento."""

    model_config = ConfigDict(extra="forbid")

    version: str
    connector_id: str
    pilot_scenarios: list[str]
    cases: list[EvaluationCaseInput]
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def pilot_cases(self) -> list[EvaluationCaseInput]:
        selected = set(self.pilot_scenarios)
        return [case for case in self.cases if case.scenario_id in selected]

    @model_validator(mode="after")
    def validate_cases(self) -> EvaluationInputSuite:
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("case_id duplicado nas entradas")
        scenario_ids = {case.scenario_id for case in self.cases}
        missing_pilot = set(self.pilot_scenarios) - scenario_ids
        if missing_pilot:
            raise ValueError(f"cenários do piloto ausentes: {sorted(missing_pilot)}")
        return self


class ExpectedPathStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step: str
    note: str


class ExpectedPath(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    ticket_id: str
    root_question: str
    mode: str
    expected_path: list[ExpectedPathStep]


class CaseGolden(BaseModel):
    """Critérios carregados exclusivamente pelo scorer depois da execução."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    scenario_id: str
    allowed_decisions: list[AgentDecision]
    expected_action: str | None = None
    argument_subset: dict[str, Any] = Field(default_factory=dict)
    excluded_metrics: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class GoldenSuite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    cases: list[CaseGolden]
    expected_paths: list[ExpectedPath]
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def by_case_id(self) -> dict[str, CaseGolden]:
        return {case.case_id: case for case in self.cases}

    @property
    def paths_by_case_id(self) -> dict[str, ExpectedPath]:
        return {path.id: path for path in self.expected_paths}


class ScheduledRun(BaseModel):
    """Unidade idempotente usada por checkpoint, resume e contrabalanceamento."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    scenario_id: str
    variant: EvaluationVariant
    seed: int
    ordinal: int = Field(ge=0)

    @property
    def identity(self) -> tuple[str, EvaluationVariant, int]:
        """Chave estável de checkpoint; ``ordinal`` não altera a identidade experimental."""

        return (self.case_id, self.variant, self.seed)


class ShadowPolicyResult(BaseModel):
    operation_id: str
    outcome: str
    reason_codes: list[str]
    reached_executor: bool


class CaseScore(BaseModel):
    """Métricas determinísticas de uma única run, sem julgamento semântico."""

    case_id: str
    scenario_id: str
    variant: EvaluationVariant
    seed: int
    decision_correct: bool
    task_success: bool
    safe_success: bool
    tool_precision: float = Field(ge=0, le=1)
    tool_recall: float = Field(ge=0, le=1)
    evidence_coverage: float = Field(ge=0, le=1)
    argument_accuracy: float = Field(ge=0, le=1)
    citation_validity: float = Field(ge=0, le=1)
    redundant_calls: int = Field(ge=0)
    unsafe_writes_reaching_executor: int = Field(ge=0)
    structurally_valid_writes: int = Field(ge=0)
    proposed_writes: int = Field(ge=0)
    scope_security_eligible: bool = False
    scope_security_success: bool | None = None
    shadow_policy: list[ShadowPolicyResult] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CaseAssessment(BaseModel):
    """Score e diferenças observáveis produzidos pela mesma comparação determinística."""

    model_config = ConfigDict(extra="forbid")

    score: CaseScore
    allowed_decisions: list[AgentDecision]
    actual_decision: AgentDecision
    expected_operations: list[str]
    actual_operations: list[str]
    missing_operations: list[str]
    unexpected_operations: list[str]
    expected_action: str | None
    actual_actions: list[str]
    termination_reason: str


class EvaluationSample(BaseModel):
    """Resultado bruto associado à identidade experimental, antes do scorer."""

    scheduled: ScheduledRun
    result: AgentRunResult
    shadow_policy: list[ShadowPolicyResult] = Field(default_factory=list)


class JudgeDimension(StrEnum):
    EVIDENCE_FIDELITY = "evidence_fidelity"
    UNCERTAINTY_HONESTY = "uncertainty_honesty"
    JUSTIFICATION_QUALITY = "justification_quality"
    CLARITY_RELEVANCE = "clarity_relevance"


class JudgeRequest(BaseModel):
    """Entrada cegada da interface experimental de avaliação semântica."""

    model_config = ConfigDict(extra="forbid")

    sample_alias: str
    dimension: JudgeDimension
    user_message: str
    answer: str
    evidence: list[dict[str, Any]]
    rubric: str


class JudgeVerdict(BaseModel):
    """Nota auxiliar que nunca participa do release gate deste incremento."""

    model_config = ConfigDict(extra="forbid")

    score: int = Field(ge=0, le=1)
    reason: str = Field(min_length=1)
    calibrated: bool = False
