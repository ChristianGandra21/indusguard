"""Exportação cegada para revisão humana independente das métricas automáticas."""

from __future__ import annotations

import csv
import hashlib
import json
import random
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from indusguard_evals.contracts import EvaluationInputSuite, EvaluationSample
from indusguard_evals.repository import PersistedEvaluationRun


class HumanReviewImportError(ValueError):
    """CSV, chave ou avaliação não formam um lote de revisão íntegro."""


class ReviewMethod(StrEnum):
    HUMAN = "human"
    ASSISTED = "assisted"


class ImportedReviewScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_alias: str
    case_id: str
    scenario_id: str
    variant: str
    seed: int
    agent_run_id: str
    evidence_fidelity: int = Field(ge=0, le=1)
    uncertainty_honesty: int = Field(ge=0, le=1)
    justification_quality: int = Field(ge=0, le=1)
    clarity_relevance: int = Field(ge=0, le=1)


class HumanReviewBundle(BaseModel):
    """Notas escalares e digests; conteúdo revisado permanece somente no CSV local."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["human-review-bundle-v1"] = "human-review-bundle-v1"
    evaluation_id: str
    dataset_version: str
    input_digest: str
    golden_digest: str
    model: str
    git_commit: str
    review_method: ReviewMethod
    calibrated: Literal[False] = False
    rubric_version: str
    source_csv_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rubric_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sample_count: int = Field(ge=1)
    samples: list[ImportedReviewScore]
    aggregates: dict[str, float]
    bundle_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


_REVIEW_COLUMNS = {
    "evidence_fidelity_0_or_1": "evidence_fidelity",
    "uncertainty_honesty_0_or_1": "uncertainty_honesty",
    "justification_quality_0_or_1": "justification_quality",
    "clarity_relevance_0_or_1": "clarity_relevance",
}


def export_human_review(
    samples: list[EvaluationSample],
    inputs: EvaluationInputSuite,
    output_path: Path,
    *,
    random_seed: int = 20260823,
) -> Path:
    """Escreve CSV sem nome da variante e devolve uma chave separada para reconciliação."""

    case_by_id = {case.case_id: case for case in inputs.cases}
    shuffled = list(samples)
    random.Random(random_seed).shuffle(shuffled)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    key_path = output_path.with_name(f"{output_path.stem}-key.json")
    key: dict[str, dict[str, str | int]] = {}
    fieldnames = [
        "sample_alias",
        "scenario_id",
        "user_message",
        "answer",
        "evidence",
        "evidence_fidelity_0_or_1",
        "uncertainty_honesty_0_or_1",
        "justification_quality_0_or_1",
        "clarity_relevance_0_or_1",
        "reviewer_notes",
    ]
    with output_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for sample in shuffled:
            identity = (
                f"{sample.scheduled.case_id}:{sample.scheduled.variant.value}:"
                f"{sample.scheduled.seed}"
            )
            alias = f"sample-{hashlib.sha256(identity.encode()).hexdigest()[:10]}"
            case = case_by_id[sample.scheduled.case_id]
            writer.writerow(
                {
                    "sample_alias": alias,
                    "scenario_id": sample.scheduled.scenario_id,
                    "user_message": case.message,
                    "answer": sample.result.answer,
                    "evidence": json.dumps(
                        [item.model_dump(mode="json") for item in sample.result.evidence],
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "evidence_fidelity_0_or_1": "",
                    "uncertainty_honesty_0_or_1": "",
                    "justification_quality_0_or_1": "",
                    "clarity_relevance_0_or_1": "",
                    "reviewer_notes": "",
                }
            )
            key[alias] = {
                "case_id": sample.scheduled.case_id,
                "variant": sample.scheduled.variant.value,
                "seed": sample.scheduled.seed,
                "agent_run_id": sample.result.run_id,
            }
    key_path.write_text(
        json.dumps(key, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return key_path


def import_human_review(
    run: PersistedEvaluationRun,
    samples: list[EvaluationSample],
    *,
    csv_path: Path,
    key_path: Path,
    rubric_path: Path,
    review_method: ReviewMethod,
) -> HumanReviewBundle:
    """Valida um CSV preenchido e devolve um bundle que não replica conteúdo revisado."""

    try:
        key_payload = json.loads(key_path.read_text(encoding="utf-8"))
        rubric_payload = yaml.safe_load(rubric_path.read_text(encoding="utf-8"))
        with csv_path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
    except (OSError, ValueError, csv.Error, yaml.YAMLError) as exc:
        raise HumanReviewImportError(
            "HUMAN_REVIEW_INVALID: não foi possível ler CSV, chave ou rubrica"
        ) from exc
    if not isinstance(key_payload, dict) or not isinstance(rubric_payload, dict):
        raise HumanReviewImportError("HUMAN_REVIEW_INVALID: chave ou rubrica inválida")
    if len(rows) != len(samples) or not rows:
        raise HumanReviewImportError(
            "HUMAN_REVIEW_INVALID: CSV precisa cobrir todos os checkpoints exatamente uma vez"
        )
    aliases = [row.get("sample_alias", "") for row in rows]
    if len(set(aliases)) != len(aliases) or set(aliases) != set(key_payload):
        raise HumanReviewImportError(
            "HUMAN_REVIEW_INVALID: aliases do CSV e da chave precisam coincidir sem duplicatas"
        )

    identities = {
        (
            sample.scheduled.case_id,
            sample.scheduled.variant.value,
            sample.scheduled.seed,
            sample.result.run_id,
        ): sample
        for sample in samples
    }
    imported: list[ImportedReviewScore] = []
    for row in rows:
        alias = row["sample_alias"]
        key_item = key_payload.get(alias)
        if not isinstance(key_item, dict):
            raise HumanReviewImportError("HUMAN_REVIEW_INVALID: entrada da chave inválida")
        try:
            identity = (
                str(key_item["case_id"]),
                str(key_item["variant"]),
                int(key_item["seed"]),
                str(key_item["agent_run_id"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HumanReviewImportError(
                "HUMAN_REVIEW_INVALID: identidade incompleta na chave"
            ) from exc
        sample = identities.get(identity)
        if sample is None or row.get("scenario_id") != sample.scheduled.scenario_id:
            raise HumanReviewImportError(
                "HUMAN_REVIEW_INVALID: chave pertence a outra avaliação ou cenário"
            )
        scores: dict[str, int] = {}
        for source, target in _REVIEW_COLUMNS.items():
            raw = row.get(source)
            if raw not in {"0", "1"}:
                raise HumanReviewImportError(
                    f"HUMAN_REVIEW_INVALID: {source} aceita somente 0 ou 1"
                )
            scores[target] = int(raw)
        imported.append(
            ImportedReviewScore(
                sample_alias=alias,
                case_id=sample.scheduled.case_id,
                scenario_id=sample.scheduled.scenario_id,
                variant=sample.scheduled.variant.value,
                seed=sample.scheduled.seed,
                agent_run_id=sample.result.run_id,
                **scores,
            )
        )
    imported.sort(key=lambda item: item.sample_alias)
    rubric_version = rubric_payload.get("version")
    if not isinstance(rubric_version, str) or not rubric_version:
        raise HumanReviewImportError("HUMAN_REVIEW_INVALID: rubrica sem versão")
    aggregates = {
        dimension: sum(getattr(item, dimension) for item in imported) / len(imported)
        for dimension in _REVIEW_COLUMNS.values()
    }
    payload = {
        "schema_version": "human-review-bundle-v1",
        "evaluation_id": run.evaluation_id,
        "dataset_version": run.dataset_version,
        "input_digest": run.input_digest,
        "golden_digest": run.golden_digest or "",
        "model": run.model,
        "git_commit": run.git_commit,
        "review_method": review_method,
        "calibrated": False,
        "rubric_version": rubric_version,
        "source_csv_sha256": _file_digest(csv_path),
        "key_sha256": _file_digest(key_path),
        "rubric_sha256": _file_digest(rubric_path),
        "sample_count": len(imported),
        "samples": [item.model_dump(mode="json") for item in imported],
        "aggregates": aggregates,
    }
    bundle_digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return HumanReviewBundle.model_validate({**payload, "bundle_digest": bundle_digest})


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
