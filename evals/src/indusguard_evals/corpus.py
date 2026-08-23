"""Carregamento em duas fases para impedir vazamento do golden set ao agente."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, TypeAdapter

from indusguard_evals.contracts import (
    CaseGolden,
    EvaluationCaseInput,
    EvaluationInputSuite,
    ExpectedPath,
    GoldenSuite,
    RunContextEntry,
    StakeholderCase,
)


class CorpusValidationError(ValueError):
    """Indica drift ou inconsistência entre as partes versionadas do corpus."""


class _RunContexts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    connector_id: str
    pilot_scenarios: list[str]
    cases: list[RunContextEntry]


class _ScenarioGoldens(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    cases: list[CaseGolden]


def _digest_files(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CorpusValidationError(f"{path} precisa conter um objeto YAML")
    return value


class OfficialCorpus:
    """Fachada que torna explícito quando o processo cruza a fronteira do golden set."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def load_inputs(self) -> EvaluationInputSuite:
        """Lê somente arquivos autorizados antes da execução do agente."""

        input_path = self._root / "inputs.json"
        context_path = self._root / "run-contexts.yaml"
        raw_cases = TypeAdapter(list[StakeholderCase]).validate_json(
            input_path.read_text(encoding="utf-8")
        )
        contexts = _RunContexts.model_validate(_load_yaml(context_path))
        context_by_id = {entry.case_id: entry for entry in contexts.cases}
        input_ids = [case.id for case in raw_cases]
        if len(context_by_id) != len(contexts.cases):
            raise CorpusValidationError("run-contexts contém case_id duplicado")
        if set(input_ids) != set(context_by_id):
            raise CorpusValidationError("inputs e run-contexts precisam conter os mesmos casos")

        cases = []
        for raw in raw_cases:
            context = context_by_id[raw.id]
            cases.append(
                EvaluationCaseInput(
                    case_id=raw.id,
                    ticket_id=raw.ticket_id,
                    scenario_id=context.scenario_id,
                    connector_id=contexts.connector_id,
                    company_id=raw.company_id,
                    user_id=raw.user_id,
                    asset_id=raw.asset_id,
                    message=raw.message,
                    direct_request=context.direct_request,
                )
            )
        return EvaluationInputSuite(
            version=contexts.version,
            connector_id=contexts.connector_id,
            pilot_scenarios=contexts.pilot_scenarios,
            cases=cases,
            digest=_digest_files([input_path, context_path]),
        )

    def load_goldens(self, inputs: EvaluationInputSuite) -> GoldenSuite:
        """Cruza a fronteira do gold somente depois que o runner produziu resultados."""

        scenario_path = self._root / "goldens" / "scenarios.yaml"
        expected_path = self._root / "goldens" / "expected-paths.json"
        scenario_goldens = _ScenarioGoldens.model_validate(_load_yaml(scenario_path))
        expected_paths = TypeAdapter(list[ExpectedPath]).validate_json(
            expected_path.read_text(encoding="utf-8")
        )
        input_by_id = {case.case_id: case for case in inputs.cases}
        golden_by_id = {case.case_id: case for case in scenario_goldens.cases}
        path_by_id = {path.id: path for path in expected_paths}
        if len(golden_by_id) != len(scenario_goldens.cases):
            raise CorpusValidationError("scenarios.yaml contém case_id duplicado")
        if len(path_by_id) != len(expected_paths):
            raise CorpusValidationError("expected-paths.json contém id duplicado")
        expected_ids = set(input_by_id)
        if expected_ids != set(golden_by_id) or expected_ids != set(path_by_id):
            raise CorpusValidationError(
                "inputs e goldens precisam cobrir exatamente os mesmos casos"
            )
        for case_id, golden in golden_by_id.items():
            if golden.scenario_id != input_by_id[case_id].scenario_id:
                raise CorpusValidationError(f"scenario_id divergente para {case_id}")
        return GoldenSuite(
            version=scenario_goldens.version,
            cases=scenario_goldens.cases,
            expected_paths=expected_paths,
            digest=_digest_files([scenario_path, expected_path]),
        )
