"""O manifesto vincula consentimento sem copiar payloads ou abrir fronteiras externas."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from indusguard_api.groq_gateway import GroqAgentSettings

from indusguard_evals import schedule as schedule_module
from indusguard_evals.pacing import GroqPilotPacingSettings
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


def test_manifest_rejects_tampering_and_live_configuration_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_git(monkeypatch)
    output = tmp_path / "preflight.json"
    manifest = write_groq_pilot_preflight(REPOSITORY_ROOT, output, _settings())

    loaded = load_and_validate_groq_pilot_preflight(REPOSITORY_ROOT, output, _settings())
    assert loaded.manifest_digest == manifest.manifest_digest

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
