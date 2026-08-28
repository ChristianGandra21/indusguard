"""O CLI valida o snapshot sem banco, Groq ou rede externa."""

import json
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

import indusguard_evals.cli as eval_cli
from indusguard_evals.cli import _requested_execution_kind, main
from indusguard_evals.contracts import EvaluationExecutionKind
from indusguard_evals.corpus import OfficialCorpus


def test_preflight_writes_auditable_metadata_without_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = "a" * 40
    original_check_output = subprocess.check_output

    def git_output(command: list[str], **kwargs: object) -> str:
        if command == ["git", "rev-parse", "HEAD"]:
            return commit
        if command == ["git", "status", "--porcelain"]:
            return ""
        return original_check_output(command, **kwargs)

    monkeypatch.setattr(subprocess, "check_output", git_output)
    monkeypatch.setenv("GROQ_API_KEY", "must-never-be-serialized")
    monkeypatch.setattr(
        eval_cli,
        "_gateway",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("preflight não pode construir gateway")
        ),
    )
    monkeypatch.setattr(
        OfficialCorpus,
        "load_goldens",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("preflight não pode abrir goldens")
        ),
    )
    output = tmp_path / "groq-pilot-preflight.json"

    assert main(["preflight", "--groq", "--output", str(output)]) == 0

    payload = json.loads(output.read_text(encoding="utf-8"))
    serialized = json.dumps(payload, ensure_ascii=False)
    assert payload["schema_version"] == "groq-pilot-preflight-v1"
    assert payload["repository"] == {"git_commit": commit, "worktree_clean": True}
    assert payload["corpus"]["version"] == "official-v1"
    assert payload["corpus"]["pilot_scenarios"] == ["CEN-01", "CEN-14"]
    assert len(payload["schedule"]) == 12
    assert {item["seed"] for item in payload["schedule"]} == {11, 42, 73}
    assert {item["variant"] for item in payload["schedule"]} == {
        "prompt_only",
        "guarded",
    }
    assert len(payload["messages"]) == 2
    assert all(len(item["sha256"]) == 64 for item in payload["messages"])
    assert all(item["utf8_bytes"] > 0 for item in payload["messages"])
    assert "O redutor da correia" not in serialized
    assert "Esse compressor tá" not in serialized
    assert "must-never-be-serialized" not in serialized
    assert len(payload["manifest_digest"]) == 64


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


def test_groq_pilot_and_resume_require_preflight_before_external_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_gateway(*args: object, **kwargs: object) -> object:
        raise AssertionError("o gateway não pode nascer antes do manifesto")

    monkeypatch.setattr(eval_cli, "_gateway", unexpected_gateway)
    for command in (
        ["pilot", "--groq", "--confirm-external-transmission"],
        [
            "resume",
            "11111111-1111-4111-8111-111111111111",
            "--groq",
            "--confirm-external-transmission",
        ],
    ):
        with pytest.raises(SystemExit, match="PILOT_PREFLIGHT_REQUIRED"):
            main(command)


def test_fake_mode_rejects_a_preflight_manifest() -> None:
    with pytest.raises(SystemExit, match="PREFLIGHT_MODE_MISMATCH"):
        main(["pilot", "--fake", "--preflight-manifest", "preflight.json"])


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
