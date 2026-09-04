"""Patch opt-in do ciclo de melhoria, restrito ao pacote de avaliação."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from indusguard_evals.analysis import FailureCategory, ImprovementPlan


class ImprovementPatchError(ValueError):
    """O patch não pode ser aplicado sem quebrar as fronteiras auditáveis."""


class AppliedImprovementRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    status: Literal["applied", "already_applied"]
    changed_files: list[str] = Field(default_factory=list)
    details: list[str] = Field(default_factory=list)


class ImprovementPatchResult(BaseModel):
    """Resultado auditável de uma aplicação local e opt-in."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["improvement-patch-v1"] = "improvement-patch-v1"
    generated_at: datetime
    evaluation_id: str
    base_commit: str
    recipes: list[AppliedImprovementRecipe]
    changed_files: list[str]
    validation_commands: list[str]
    next_steps: list[str]

    def to_markdown_section(self) -> str:
        lines = [
            "## Patch opt-in aplicado",
            "",
            f"- Schema: `{self.schema_version}`",
            f"- Base commit: `{self.base_commit}`",
            f"- Arquivos alterados: {_code_list(self.changed_files)}",
            "",
            "### Receitas",
            "",
        ]
        for recipe in self.recipes:
            lines.append(
                f"- `{recipe.name}`: `{recipe.status}`; arquivos: "
                f"{_code_list(recipe.changed_files)}"
            )
        lines.extend(["", "### Validação obrigatória", ""])
        lines.extend(f"- `{command}`" for command in self.validation_commands)
        lines.extend(["", "### Próximos passos", ""])
        lines.extend(f"- {step}" for step in self.next_steps)
        lines.append("")
        return "\n".join(lines)


class ImprovementPatchPlanner:
    """Escolhe receitas determinísticas a partir das falhas observadas."""

    _GUIDANCE_CATEGORIES = frozenset(
        {
            FailureCategory.DECISION_INCORRECT,
            FailureCategory.MISSING_EVIDENCE,
            FailureCategory.EXPECTED_ACTION_MISSING,
            FailureCategory.INCORRECT_ACTION,
            FailureCategory.ARGUMENT_INCORRECT,
            FailureCategory.CITATION_INVALID,
        }
    )

    def recipes_for(self, plan: ImprovementPlan) -> list[str]:
        categories = {category for finding in plan.findings for category in finding.categories}
        recipes: list[str] = []
        if categories & self._GUIDANCE_CATEGORIES:
            recipes.append("agent-guidance-recipe")
        if plan.benchmark_criteria.get("prompt_only_more_unsafe_than_guarded") is False:
            recipes.append("baseline-contrast-recipe")
        return recipes


class ImprovementPatchWriter:
    """Aplica somente receitas allowlisted em um checkout limpo e compatível."""

    validation_commands = [
        "make eval-validate",
        (
            ".venv/bin/pytest evals/tests/test_analysis.py "
            "evals/tests/test_improvement_cli.py evals/tests/test_variant_runtime.py "
            "apps/api/tests/test_agent_runtime.py -q"
        ),
        "make eval-pilot-fake",
    ]

    def __init__(self, root: Path) -> None:
        self._root = root

    def apply(self, plan: ImprovementPlan) -> ImprovementPatchResult:
        self._validate_checkout(plan.git_commit)
        recipes = ImprovementPatchPlanner().recipes_for(plan)
        results = [self._apply_recipe(name) for name in recipes]
        changed_files = sorted({path for recipe in results for path in recipe.changed_files})
        return ImprovementPatchResult(
            generated_at=datetime.now(UTC),
            evaluation_id=plan.evaluation_id,
            base_commit=plan.git_commit,
            recipes=results,
            changed_files=changed_files,
            validation_commands=self.validation_commands,
            next_steps=[
                "Revise o diff local e faça commit manual se o patch estiver correto.",
                "Gere um novo preflight após o commit; manifestos antigos ficam inválidos.",
                ("Rode o piloto externo separadamente apenas com --confirm-external-transmission."),
            ],
        )

    def _apply_recipe(self, name: str) -> AppliedImprovementRecipe:
        if name == "agent-guidance-recipe":
            return self._apply_agent_guidance_recipe()
        if name == "baseline-contrast-recipe":
            return self._apply_baseline_contrast_recipe()
        raise ImprovementPatchError(f"IMPROVEMENT_RECIPE_UNKNOWN: {name}")

    def _apply_agent_guidance_recipe(self) -> AppliedImprovementRecipe:
        path = self._safe_path("connectors/tractian/domain.yaml")
        content = path.read_text(encoding="utf-8")
        marker = (
            "Não invente identificadores técnicos: analysisId deve aparecer em "
            "evidência observada antes de getAnalysis, reprocessAnalysis ou "
            "requestSpecialistAnalysis."
        )
        if marker in content:
            return AppliedImprovementRecipe(
                name="agent-guidance-recipe",
                status="already_applied",
                details=["guidance de analysisId observado já existe"],
            )
        anchor = "nunca use case_id, asset_id ou id de baseline como analysisId."
        if anchor not in content:
            raise ImprovementPatchError(
                "IMPROVEMENT_PATCH_ANCHOR_NOT_FOUND: connectors/tractian/domain.yaml"
            )
        path.write_text(content.replace(anchor, f"{anchor} {marker}", 1), encoding="utf-8")
        return AppliedImprovementRecipe(
            name="agent-guidance-recipe",
            status="applied",
            changed_files=[self._relative(path)],
            details=["reforçou uso de analysisId observado no domínio"],
        )

    def _apply_baseline_contrast_recipe(self) -> AppliedImprovementRecipe:
        path = self._safe_path("evals/src/indusguard_evals/execution.py")
        content = path.read_text(encoding="utf-8")
        marker = 'restrict_tools_to_intent": False'
        if marker in content:
            return AppliedImprovementRecipe(
                name="baseline-contrast-recipe",
                status="already_applied",
                details=["baseline prompt_only já relaxa filtro de intent"],
            )
        anchor = "        config=runtime_config,\n"
        replacement = (
            "        config=(runtime_config or AgentRuntimeConfig()).model_copy(\n"
            '            update={"restrict_tools_to_intent": False}\n'
            "        )\n"
            "        if variant is EvaluationVariant.PROMPT_ONLY\n"
            "        else runtime_config,\n"
        )
        if anchor not in content:
            raise ImprovementPatchError(
                "IMPROVEMENT_PATCH_ANCHOR_NOT_FOUND: evals/src/indusguard_evals/execution.py"
            )
        path.write_text(content.replace(anchor, replacement, 1), encoding="utf-8")
        return AppliedImprovementRecipe(
            name="baseline-contrast-recipe",
            status="applied",
            changed_files=[self._relative(path)],
            details=["baseline prompt_only passa a observar tools fora do intent classificado"],
        )

    def _validate_checkout(self, expected_commit: str) -> None:
        head = self._git("rev-parse", "HEAD").strip()
        if head != expected_commit:
            raise ImprovementPatchError("IMPROVEMENT_PATCH_COMMIT_MISMATCH")
        status = self._git("status", "--porcelain", "--untracked-files=no").strip()
        if status:
            raise ImprovementPatchError("IMPROVEMENT_PATCH_DIRTY_WORKTREE")

    def _safe_path(self, relative_path: str) -> Path:
        path = (self._root / relative_path).resolve()
        root = self._root.resolve()
        if path == root or root not in path.parents:
            raise ImprovementPatchError("IMPROVEMENT_PATCH_PATH_OUTSIDE_ROOT")
        normalized = self._relative(path)
        forbidden_prefixes = ("evals/corpus/", "deploy/")
        forbidden_parts = ("/goldens/", "/migrations/")
        allowed_prefixes = (
            "connectors/",
            "apps/api/src/indusguard_api/",
            "apps/api/tests/",
            "evals/src/indusguard_evals/",
            "evals/tests/",
        )
        if (
            normalized.startswith(".env")
            or normalized.startswith(forbidden_prefixes)
            or any(part in f"/{normalized}/" for part in forbidden_parts)
            or not normalized.startswith(allowed_prefixes)
        ):
            raise ImprovementPatchError(f"IMPROVEMENT_PATCH_PATH_FORBIDDEN: {normalized}")
        return path

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self._root.resolve()).as_posix()

    def _git(self, *args: str) -> str:
        try:
            return subprocess.check_output(
                ["git", *args],
                cwd=self._root,
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ImprovementPatchError("IMPROVEMENT_PATCH_GIT_UNAVAILABLE") from exc


def _code_list(values: list[str]) -> str:
    return ", ".join(f"`{item}`" for item in values) if values else "nenhum"
