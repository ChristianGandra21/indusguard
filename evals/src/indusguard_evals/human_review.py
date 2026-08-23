"""Exportação cegada para revisão humana independente das métricas automáticas."""

from __future__ import annotations

import csv
import hashlib
import json
import random
from pathlib import Path

from indusguard_evals.contracts import EvaluationInputSuite, EvaluationSample


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
