"""O CLI valida o snapshot sem banco, Groq ou rede externa."""

from argparse import Namespace

import pytest

from indusguard_evals.cli import _requested_execution_kind, main
from indusguard_evals.contracts import EvaluationExecutionKind


def test_validate_reports_17_tickets_and_16_scenarios(capsys: object) -> None:
    assert main(["validate"]) == 0

    output = capsys.readouterr().out
    assert "17 tickets" in output
    assert "16 cenários" in output


def _args(*, fake: bool = False, groq: bool = False, consent: bool = False) -> Namespace:
    return Namespace(
        fake=fake,
        groq=groq,
        confirm_external_transmission=consent,
    )


def test_groq_pilot_requires_explicit_external_transmission_consent() -> None:
    with pytest.raises(SystemExit, match="EXTERNAL_TRANSMISSION_CONSENT_REQUIRED"):
        _requested_execution_kind(_args(groq=True), command="pilot")

    kind = _requested_execution_kind(_args(groq=True, consent=True), command="pilot")

    assert kind is EvaluationExecutionKind.GROQ_PILOT


def test_full_groq_benchmark_remains_blocked_even_with_consent() -> None:
    with pytest.raises(SystemExit, match="FULL_BENCHMARK_NOT_AUTHORIZED"):
        _requested_execution_kind(_args(groq=True, consent=True), command="run")


def test_fake_and_groq_cannot_be_combined() -> None:
    with pytest.raises(SystemExit, match="EVALUATION_MODE_CONFLICT"):
        _requested_execution_kind(_args(fake=True, groq=True, consent=True), command="pilot")


def test_fake_smoke_does_not_accept_external_transmission_consent() -> None:
    with pytest.raises(SystemExit, match="EXTERNAL_TRANSMISSION_MODE_REQUIRED"):
        _requested_execution_kind(_args(fake=True, consent=True), command="pilot")

    kind = _requested_execution_kind(_args(fake=True), command="pilot")
    assert kind is EvaluationExecutionKind.OFFLINE_SMOKE
