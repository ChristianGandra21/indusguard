"""O CLI valida o snapshot sem banco, Groq ou rede externa."""

from indusguard_evals.cli import main


def test_validate_reports_17_tickets_and_16_scenarios(capsys: object) -> None:
    assert main(["validate"]) == 0

    output = capsys.readouterr().out
    assert "17 tickets" in output
    assert "16 cenários" in output
