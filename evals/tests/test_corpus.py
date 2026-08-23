"""Contratos do corpus oficial e isolamento físico do golden set."""

from pathlib import Path

from indusguard_evals.corpus import OfficialCorpus

CORPUS_ROOT = Path(__file__).resolve().parents[1] / "corpus" / "official-v1"


def test_inputs_load_without_reading_or_requiring_goldens(tmp_path: Path) -> None:
    """O host do agente funciona com entradas/contexto mesmo quando o gold não existe."""

    (tmp_path / "inputs.json").write_bytes((CORPUS_ROOT / "inputs.json").read_bytes())
    (tmp_path / "run-contexts.yaml").write_bytes((CORPUS_ROOT / "run-contexts.yaml").read_bytes())

    inputs = OfficialCorpus(tmp_path).load_inputs()

    assert len(inputs.cases) == 17
    assert len({case.scenario_id for case in inputs.cases}) == 16
    cen_07 = [case.case_id for case in inputs.cases if case.scenario_id == "CEN-07"]
    assert cen_07 == ["case_tkt_inv_09", "case_tkt_exe_12"]
    assert [case.scenario_id for case in inputs.pilot_cases] == ["CEN-01", "CEN-14"]


def test_goldens_match_every_input_and_preserve_known_scope_anomaly() -> None:
    """O scorer recebe cobertura total sem corrigir silenciosamente a fixture inconsistente."""

    corpus = OfficialCorpus(CORPUS_ROOT)
    inputs = corpus.load_inputs()
    goldens = corpus.load_goldens(inputs)

    assert len(goldens.cases) == 17
    anomalous = goldens.by_case_id["case_tkt_exe_15"]
    assert anomalous.excluded_metrics == ["scope_security"]
    assert anomalous.warnings == ["STAKEHOLDER_COMPANY_MISMATCH"]
    assert len(goldens.expected_paths) == 17
    assert len(goldens.digest) == 64
