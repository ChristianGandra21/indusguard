"""A planilha humana é aleatória, reproduzível e não revela a variante."""

import csv
import json
from pathlib import Path

from indusguard_evals.contracts import EvaluationSample, EvaluationVariant, ScheduledRun
from indusguard_evals.corpus import OfficialCorpus
from indusguard_evals.human_review import export_human_review
from indusguard_evals.judge import DisabledExternalJudgeGateway, ExternalJudgeNotEnabled
from tests.factories import agent_result

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CORPUS_ROOT = REPOSITORY_ROOT / "evals" / "corpus" / "official-v1"


def test_review_csv_is_blinded_and_key_is_kept_separate(tmp_path: Path) -> None:
    inputs = OfficialCorpus(CORPUS_ROOT).load_inputs()
    samples = [
        EvaluationSample(
            scheduled=ScheduledRun(
                case_id="case_tkt_inv_04",
                scenario_id="CEN-01",
                variant=variant,
                seed=11,
                ordinal=index,
            ),
            result=agent_result().model_copy(
                update={"run_id": f"00000000-0000-0000-0000-00000000000{index + 1}"}
            ),
        )
        for index, variant in enumerate(EvaluationVariant)
    ]
    output = tmp_path / "human-review.csv"

    key_path = export_human_review(samples, inputs, output, random_seed=7)

    with output.open(encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    serialized = output.read_text(encoding="utf-8")
    key = json.loads(key_path.read_text(encoding="utf-8"))
    assert len(rows) == 2
    assert "prompt_only" not in serialized
    assert "guarded" not in serialized
    assert {item["variant"] for item in key.values()} == {"prompt_only", "guarded"}
    assert set(key) == {row["sample_alias"] for row in rows}


def test_external_judge_is_disabled_by_default_without_sending_payload() -> None:
    import asyncio

    async def exercise() -> None:
        gateway = DisabledExternalJudgeGateway()
        try:
            await gateway.judge(None)
        except ExternalJudgeNotEnabled:
            return
        raise AssertionError("o judge externo deveria permanecer bloqueado")

    asyncio.run(exercise())
