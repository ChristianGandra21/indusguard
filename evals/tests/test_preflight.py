"""O manifesto vincula consentimento sem copiar payloads ou abrir fronteiras externas."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from indusguard_api.groq_gateway import GroqAgentSettings

from indusguard_evals import schedule as schedule_module
from indusguard_evals.pacing import GroqPilotPacingSettings
from indusguard_evals.pilot_models import PilotFallbackSettings
from indusguard_evals.preflight import (
    TRANSMITTED_CATEGORIES,
    PreflightError,
    load_and_validate_groq_pilot_preflight,
    require_persisted_preflight_digest,
    write_groq_pilot_preflight,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _clean_git(monkeypatch: pytest.MonkeyPatch, commit: str = "a" * 40) -> None:
    original_check_output = subprocess.check_output

    def git_output(command: list[str], **kwargs: object) -> str:
        if command == ["git", "rev-parse", "HEAD"]:
            return commit
        if command == ["git", "status", "--porcelain"]:
            return ""
        return original_check_output(command, **kwargs)

    monkeypatch.setattr(subprocess, "check_output", git_output)


def _settings(**overrides: object) -> GroqAgentSettings:
    values = {"GROQ_API_KEY": "test-key", **overrides}
    return GroqAgentSettings(**values)  # type: ignore[arg-type]


def _pacing(seconds: float = 60) -> GroqPilotPacingSettings:
    return GroqPilotPacingSettings(
        INDUSGUARD_EVAL_GROQ_MIN_REQUEST_INTERVAL_SECONDS=seconds,
        _env_file=None,
    )


def _fallback(**overrides: object) -> PilotFallbackSettings:
    values = {
        "INDUSGUARD_EVAL_FALLBACK_PROVIDERS": "eloagents,gemini",
        "ELOAGENTS_API_KEY": "elo-secret",
        "INDUSGUARD_EVAL_ELOAGENTS_BASE_URL": "https://elo.example/v1",
        "INDUSGUARD_EVAL_ELOAGENTS_MODEL": "elo-model",
        "GEMINI_API_KEY": "gemini-secret",
        "INDUSGUARD_EVAL_GEMINI_MODEL": "gemini-model",
        **overrides,
    }
    return PilotFallbackSettings(**values, _env_file=None)  # type: ignore[arg-type]


def test_manifest_binds_fallback_order_without_serializing_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_git(monkeypatch)
    output = tmp_path / "preflight.json"

    manifest = write_groq_pilot_preflight(
        REPOSITORY_ROOT,
        output,
        _settings(),
        fallback_settings=_fallback(),
    )

    assert manifest.schema_version == "groq-pilot-preflight-v4"
    assert manifest.fallback_strategy == "whole_run_restart"
    assert [item.provider for item in manifest.fallback_models] == ["eloagents", "gemini"]
    assert [item.temperature for item in manifest.fallback_models] == [0, 1]
    assert "provider_continuation_signatures" in manifest.transmission.included_categories
    assert manifest.runtime_boundaries.max_provider_attempts_per_identity == 3
    assert manifest.runtime_boundaries.maximum_identity_timeout_seconds == 1620
    serialized = output.read_text(encoding="utf-8")
    assert "elo-secret" not in serialized
    assert "gemini-secret" not in serialized
    with pytest.raises(PreflightError, match="PREFLIGHT_STALE"):
        load_and_validate_groq_pilot_preflight(
            REPOSITORY_ROOT,
            output,
            _settings(),
            fallback_settings=_fallback(INDUSGUARD_EVAL_GEMINI_MODEL="changed-model"),
        )


def test_manifest_rejects_tampering_and_live_configuration_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_git(monkeypatch)
    output = tmp_path / "preflight.json"
    manifest = write_groq_pilot_preflight(REPOSITORY_ROOT, output, _settings())

    loaded = load_and_validate_groq_pilot_preflight(REPOSITORY_ROOT, output, _settings())
    assert loaded.manifest_digest == manifest.manifest_digest
    assert loaded.runtime_boundaries.active_run_timeout_seconds == 60
    assert loaded.runtime_boundaries.paced_run_timeout_seconds == 540
    assert loaded.runtime_boundaries.max_model_calls == 8

    payload = json.loads(output.read_text(encoding="utf-8"))
    payload["schedule"][0]["seed"] = 999
    output.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PreflightError, match="PREFLIGHT_INVALID"):
        load_and_validate_groq_pilot_preflight(REPOSITORY_ROOT, output, _settings())

    write_groq_pilot_preflight(REPOSITORY_ROOT, output, _settings())
    with pytest.raises(PreflightError, match="PREFLIGHT_STALE"):
        load_and_validate_groq_pilot_preflight(
            REPOSITORY_ROOT,
            output,
            _settings(INDUSGUARD_GROQ_MODEL="different-model"),
        )

    write_groq_pilot_preflight(REPOSITORY_ROOT, output, _settings(), _pacing(60))
    with pytest.raises(PreflightError, match="PREFLIGHT_STALE"):
        load_and_validate_groq_pilot_preflight(
            REPOSITORY_ROOT,
            output,
            _settings(),
            _pacing(30),
        )


def test_preflight_rejects_missing_key_and_dirty_worktree_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "preflight.json"
    _clean_git(monkeypatch)
    with pytest.raises(PreflightError, match="MODEL_NOT_CONFIGURED"):
        write_groq_pilot_preflight(
            REPOSITORY_ROOT,
            output,
            GroqAgentSettings(GROQ_API_KEY=None),
        )
    with pytest.raises(PreflightError, match="MODEL_NOT_CONFIGURED"):
        write_groq_pilot_preflight(
            REPOSITORY_ROOT,
            output,
            GroqAgentSettings(GROQ_API_KEY="   "),
        )
    assert not output.exists()

    def dirty_git(command: list[str], **kwargs: object) -> str:
        del kwargs
        if command == ["git", "rev-parse", "HEAD"]:
            return "b" * 40
        if command == ["git", "status", "--porcelain"]:
            return " M evals/src/indusguard_evals/cli.py\n"
        raise AssertionError(command)

    monkeypatch.setattr(subprocess, "check_output", dirty_git)
    with pytest.raises(PreflightError, match="PREFLIGHT_DIRTY_WORKTREE"):
        write_groq_pilot_preflight(REPOSITORY_ROOT, output, _settings())
    assert not output.exists()


def test_resume_requires_the_same_persisted_manifest_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_git(monkeypatch)
    manifest = write_groq_pilot_preflight(
        REPOSITORY_ROOT,
        tmp_path / "preflight.json",
        _settings(),
    )

    require_persisted_preflight_digest(
        manifest,
        {"preflight_manifest_digest": manifest.manifest_digest},
    )
    with pytest.raises(PreflightError, match="PREFLIGHT_STALE"):
        require_persisted_preflight_digest(
            manifest,
            {"preflight_manifest_digest": "f" * 64},
        )


def test_manifest_becomes_stale_when_commit_corpus_schedule_or_contract_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "preflight.json"
    _clean_git(monkeypatch, "a" * 40)
    write_groq_pilot_preflight(REPOSITORY_ROOT, output, _settings())

    _clean_git(monkeypatch, "b" * 40)
    with pytest.raises(PreflightError, match="PREFLIGHT_STALE"):
        load_and_validate_groq_pilot_preflight(REPOSITORY_ROOT, output, _settings())

    _clean_git(monkeypatch, "a" * 40)
    changed_root = tmp_path / "changed-root"
    shutil.copytree(REPOSITORY_ROOT / "connectors", changed_root / "connectors")
    corpus_target = changed_root / "evals" / "corpus" / "official-v1"
    corpus_target.mkdir(parents=True)
    shutil.copy2(
        REPOSITORY_ROOT / "evals" / "corpus" / "official-v1" / "inputs.json",
        corpus_target / "inputs.json",
    )
    shutil.copy2(
        REPOSITORY_ROOT / "evals" / "corpus" / "official-v1" / "run-contexts.yaml",
        corpus_target / "run-contexts.yaml",
    )
    inputs_path = corpus_target / "inputs.json"
    inputs = json.loads(inputs_path.read_text(encoding="utf-8"))
    inputs[0]["message"] += " alteração controlada"
    inputs_path.write_text(json.dumps(inputs, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(PreflightError, match="PREFLIGHT_STALE"):
        load_and_validate_groq_pilot_preflight(changed_root, output, _settings())

    monkeypatch.setattr(schedule_module, "PILOT_SEEDS", (11, 42, 99))
    with pytest.raises(PreflightError, match="PREFLIGHT_STALE"):
        load_and_validate_groq_pilot_preflight(REPOSITORY_ROOT, output, _settings())

    monkeypatch.setattr(schedule_module, "PILOT_SEEDS", (11, 42, 73))
    monkeypatch.setattr(
        "indusguard_evals.preflight.TRANSMITTED_CATEGORIES",
        [*TRANSMITTED_CATEGORIES, "new_transmitted_category"],
    )
    with pytest.raises(PreflightError, match="PREFLIGHT_STALE"):
        load_and_validate_groq_pilot_preflight(REPOSITORY_ROOT, output, _settings())
