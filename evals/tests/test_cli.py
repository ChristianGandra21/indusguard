"""O CLI valida o snapshot sem banco, Groq ou rede externa."""

import asyncio
import json
import subprocess
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from indusguard_api.persistence import Base
from sqlalchemy.ext.asyncio import create_async_engine

import indusguard_evals.cli as eval_cli
from indusguard_evals.cli import _print_progress, _requested_execution_kind, _resume, main
from indusguard_evals.contracts import (
    EvaluationExecutionKind,
    EvaluationPhase,
    EvaluationVariant,
)
from indusguard_evals.corpus import OfficialCorpus
from indusguard_evals.pacing import PacedAgentModelGateway
from indusguard_evals.pilot_models import PilotFallbackSettings, WholeRunFallbackGateway
from indusguard_evals.report import BenchmarkInterruption, build_summary
from indusguard_evals.repository import EvaluationRepository
from indusguard_evals.runner import EvaluationProgress


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
    monkeypatch.setenv("INDUSGUARD_EVAL_FALLBACK_PROVIDERS", "")
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
    assert payload["schema_version"] == "groq-pilot-preflight-v6"
    assert payload["repository"] == {"git_commit": commit, "worktree_clean": True}
    assert payload["corpus"]["version"] == "official-v1"
    assert payload["corpus"]["pilot_scenarios"] == ["CEN-01", "CEN-14"]
    assert len(payload["schedule"]) == 12
    assert payload["model"]["minimum_request_interval_seconds"] == 60
    assert payload["fallback_strategy"] == "whole_run_restart"
    assert payload["fallback_models"] == []
    assert payload["runtime_boundaries"]["active_run_timeout_seconds"] == 60
    assert payload["runtime_boundaries"]["paced_run_timeout_seconds"] == 540
    assert payload["runtime_boundaries"]["max_model_calls"] == 8
    assert payload["runtime_boundaries"]["max_provider_attempts_per_identity"] == 1
    assert payload["runtime_boundaries"]["maximum_identity_timeout_seconds"] == 540
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


def test_only_the_groq_evaluation_gateway_receives_pacing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INDUSGUARD_EVAL_GROQ_MIN_REQUEST_INTERVAL_SECONDS", "45")
    settings = eval_cli.GroqAgentSettings(GROQ_API_KEY="test-key", _env_file=None)

    gateway = eval_cli._gateway(EvaluationExecutionKind.GROQ_PILOT, settings)
    fake = eval_cli._gateway(EvaluationExecutionKind.OFFLINE_SMOKE)

    assert isinstance(gateway, PacedAgentModelGateway)
    assert gateway.runtime_config.run_timeout_seconds == 420
    assert gateway.runtime_config.max_model_calls == 8
    assert not isinstance(fake, PacedAgentModelGateway)


def test_fallback_chain_paces_only_the_primary_groq_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INDUSGUARD_EVAL_GROQ_MIN_REQUEST_INTERVAL_SECONDS", "45")
    settings = eval_cli.GroqAgentSettings(GROQ_API_KEY="test-key", _env_file=None)
    fallbacks = PilotFallbackSettings(
        INDUSGUARD_EVAL_FALLBACK_PROVIDERS="eloagents,gemini",
        ELOAGENTS_API_KEY="elo-key",
        INDUSGUARD_EVAL_ELOAGENTS_BASE_URL="https://elo.example/v1",
        INDUSGUARD_EVAL_ELOAGENTS_MODEL="elo-model",
        GEMINI_API_KEY="gemini-key",
        INDUSGUARD_EVAL_GEMINI_MODEL="gemini-3.7-flash",
        _env_file=None,
    )

    gateway = eval_cli._gateway(EvaluationExecutionKind.GROQ_PILOT, settings, fallbacks)

    assert isinstance(gateway, WholeRunFallbackGateway)
    assert isinstance(gateway._gateways[0], PacedAgentModelGateway)
    assert all(not isinstance(item, PacedAgentModelGateway) for item in gateway._gateways[1:])
    assert gateway.runtime_config.run_timeout_seconds == 420


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


def test_probe_fallbacks_requires_explicit_external_consent() -> None:
    with pytest.raises(SystemExit, match="EXTERNAL_TRANSMISSION_CONSENT_REQUIRED"):
        main(
            [
                "probe-fallbacks",
                "--preflight-manifest",
                "preflight.json",
                "--output",
                "probe.json",
            ]
        )


def test_fallback_pilot_requires_a_probe_before_gateway_creation() -> None:
    fallbacks = PilotFallbackSettings(
        INDUSGUARD_EVAL_FALLBACK_PROVIDERS="gemini",
        GEMINI_API_KEY="gemini-key",
        INDUSGUARD_EVAL_GEMINI_MODEL="gemini-model",
        _env_file=None,
    )
    args = Namespace(provider_probe=None)

    with pytest.raises(SystemExit, match="PROVIDER_PROBE_REQUIRED"):
        eval_cli._validated_provider_probe(
            args,
            EvaluationExecutionKind.GROQ_PILOT,
            Path.cwd(),
            fallbacks,
            object(),
        )


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


def test_progress_is_safe_json_on_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    _print_progress(
        EvaluationProgress(
            evaluation_id="evaluation-1",
            completed_runs=5,
            expected_runs=12,
            checkpoint_status="completed",
            case_id="case_tkt_inv_04",
            scenario_id="CEN-01",
            variant=EvaluationVariant.GUARDED,
            seed=42,
        )
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert captured.out == ""
    assert payload == {
        "event": "evaluation_progress",
        "evaluation_id": "evaluation-1",
        "completed_runs": 5,
        "expected_runs": 12,
        "checkpoint_status": "completed",
        "case_id": "case_tkt_inv_04",
        "scenario_id": "CEN-01",
        "variant": "guarded",
        "seed": 42,
    }
    assert "message" not in payload
    assert "answer" not in payload


def test_resume_before_retry_after_stops_before_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'resume-window.db'}"
    resume_at = datetime.now(UTC) + timedelta(minutes=5)
    summary = build_summary(
        [],
        [],
        expected_runs=12,
        completed=False,
        interruption=BenchmarkInterruption(
            retry_after_seconds=300,
            resume_not_before=resume_at,
        ),
    )

    async def seed() -> str:
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        repository = EvaluationRepository(engine)
        evaluation_id = await repository.start(
            phase=EvaluationPhase.PILOT,
            dataset_version="official-v1",
            input_digest="a" * 64,
            model="openai/gpt-oss-20b",
            git_commit="b" * 40,
            config={
                "execution_kind": EvaluationExecutionKind.GROQ_PILOT.value,
                "preflight_manifest_digest": "c" * 64,
            },
        )
        await repository.finish(
            evaluation_id,
            status="partial",
            summary=summary.model_dump(mode="json"),
        )
        await engine.dispose()
        return evaluation_id

    evaluation_id = asyncio.run(seed())
    monkeypatch.setattr(
        eval_cli,
        "_validated_preflight",
        lambda *args: (None, None, object()),
    )
    monkeypatch.setattr(eval_cli, "require_persisted_preflight_digest", lambda *args: None)
    monkeypatch.setattr(
        eval_cli,
        "_gateway",
        lambda *args: (_ for _ in ()).throw(AssertionError("gateway não deve ser criado")),
    )
    args = Namespace(
        command="resume",
        fake=False,
        groq=True,
        confirm_external_transmission=True,
        preflight_manifest=Path("preflight.json"),
        database_url=database_url,
        evaluation_id=evaluation_id,
    )

    with pytest.raises(SystemExit, match="MODEL_RATE_LIMITED: retomada disponível após"):
        asyncio.run(_resume(args))

    eval_cli._enforce_resume_window(
        summary.model_dump(mode="json"),
        now=resume_at,
    )
    assert "Retry-After=300s" in eval_cli._rate_limit_guidance(summary)
    unknown_window = summary.model_copy(update={"interruption": BenchmarkInterruption()})
    assert "não informou Retry-After" in eval_cli._rate_limit_guidance(unknown_window)
