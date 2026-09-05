"""Fluxo de promoção com repositório Git real; nenhum provedor ou GitHub é chamado."""

from pathlib import Path

import pytest
from indusguard_api.improvements import ImprovementStore

from indusguard_evals.self_improvement import SelfImprovementAgent, SelfImprovementError
from tests.test_improvement_cli import _git, _patchable_plan, _seed_patch_repo


def _prepared(tmp_path):
    root = _seed_patch_repo(tmp_path / "repo")
    agent = SelfImprovementAgent(root, ImprovementStore(tmp_path / "proposals"))
    base = _git(root, "rev-parse", "HEAD")
    return root, agent, agent.prepare(_patchable_plan(base))


def _validated(tmp_path, monkeypatch):
    root, agent, record = _prepared(tmp_path)
    monkeypatch.setattr(agent, "_run_validation", lambda *args: True)
    record = agent.validate(record.proposal_id)
    return root, agent, record


def test_prepare_isolates_patch_and_never_changes_baseline_or_original_branch(tmp_path):
    root, agent, record = _prepared(tmp_path)
    assert record.status == "prepared"
    assert record.changed_files == ["connectors/tractian/domain.yaml"]
    assert record.commit_sha is None
    assert _git(root, "status", "--porcelain") == ""
    assert _git(root, "rev-parse", "HEAD") == record.base_commit
    assert _git(Path(record.worktree), "rev-parse", "HEAD") == record.base_commit
    assert _git(Path(record.worktree), "diff", "HEAD", "--", "evals") == ""
    assert agent.store.read(record.proposal_id).patch_digest == record.patch_digest


def test_commit_requires_validation_and_interactive_human_confirmation(tmp_path, monkeypatch):
    root, agent, record = _prepared(tmp_path)
    with pytest.raises(SelfImprovementError, match="HUMAN_TERMINAL_REQUIRED"):
        agent.review(record.proposal_id, confirm=lambda _: record.patch_digest, interactive=False)
    with pytest.raises(SelfImprovementError, match="REVIEW_NOT_READY"):
        agent.review(record.proposal_id, confirm=lambda _: record.patch_digest, interactive=True)
    monkeypatch.setattr(agent, "_run_validation", lambda *args: False)
    assert agent.validate(record.proposal_id).status == "validation_failed"
    with pytest.raises(SelfImprovementError, match="REVIEW_NOT_READY"):
        agent.review(record.proposal_id, confirm=lambda _: record.patch_digest, interactive=True)
    monkeypatch.setattr(agent, "_run_validation", lambda *args: True)
    assert agent.validate(record.proposal_id).status == "pending_review"
    with pytest.raises(SelfImprovementError, match="APPROVAL_CANCELLED"):
        agent.review(record.proposal_id, confirm=lambda _: "yes", interactive=True)

    def human(prompt):
        assert "analysisId deve aparecer" in prompt
        assert record.patch_digest in prompt
        return record.patch_digest

    result = agent.review(record.proposal_id, confirm=human, interactive=True)
    assert result.status == "committed"
    assert result.approved_by.startswith("Test User")
    assert result.approved_at is not None
    assert _git(root, "rev-parse", "HEAD") == record.base_commit
    assert _git(root, "rev-parse", result.branch) == result.commit_sha
    assert _git(root, "rev-parse", f"{result.commit_sha}^{{tree}}") == record.tree_sha
    assert _git(Path(record.worktree), "status", "--porcelain") == ""
    with pytest.raises(SelfImprovementError, match="REVIEW_NOT_READY"):
        agent.review(record.proposal_id, confirm=human, interactive=True)


@pytest.mark.parametrize("mutation", ["patch", "index", "untracked", "branch"])
def test_stale_candidate_cannot_be_approved(tmp_path, monkeypatch, mutation):
    root, agent, record = _validated(tmp_path, monkeypatch)
    worktree = Path(record.worktree)
    if mutation == "patch":
        with (worktree / "connectors/tractian/domain.yaml").open("a") as output:
            output.write("changed: true\n")
    elif mutation == "index":
        _git(worktree, "reset", "HEAD", "--", "connectors/tractian/domain.yaml")
    elif mutation == "untracked":
        (worktree / "unexpected.py").write_text("print('unreviewed')\n")
    else:
        _git(worktree, "checkout", "-b", "other")
    with pytest.raises(SelfImprovementError, match="IMPROVEMENT_"):
        agent.review(record.proposal_id, confirm=lambda _: record.patch_digest, interactive=True)
    assert agent.store.read(record.proposal_id).commit_sha is None
    assert _git(root, "rev-parse", "HEAD") == record.base_commit


def test_change_during_human_review_invalidates_approval(tmp_path, monkeypatch):
    _, agent, record = _validated(tmp_path, monkeypatch)

    def human(_):
        (Path(record.worktree) / "connectors/tractian/domain.yaml").write_text("tampered\n")
        return record.patch_digest

    with pytest.raises(SelfImprovementError, match="PATCH_CHANGED"):
        agent.review(record.proposal_id, confirm=human, interactive=True)
    assert agent.store.read(record.proposal_id).approved_at is None


def test_rejection_is_terminal(tmp_path, monkeypatch):
    _, agent, record = _validated(tmp_path, monkeypatch)
    result = agent.review(record.proposal_id, confirm=lambda _: "rejeitar", interactive=True)
    assert result.status == "rejected"
    assert result.commit_sha is None
    with pytest.raises(SelfImprovementError, match="INVALID_STATE"):
        agent.validate(record.proposal_id)


def test_recovers_approved_commit_without_repeating_it(tmp_path, monkeypatch):
    root, agent, record = _validated(tmp_path, monkeypatch)
    finish = agent._finish_commit
    monkeypatch.setattr(agent, "_finish_commit", lambda _: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError):
        agent.review(record.proposal_id, confirm=lambda _: record.patch_digest, interactive=True)
    pending = agent.store.read(record.proposal_id)
    assert pending.status == "committing"
    assert _git(root, "rev-parse", record.branch) == record.base_commit
    monkeypatch.setattr(agent, "_finish_commit", finish)
    result = agent.recover(record.proposal_id)
    assert result.status == "committed"
    assert result.commit_sha == pending.commit_sha
    assert _git(root, "rev-parse", record.branch) == pending.commit_sha


def test_no_applicable_recipe_never_creates_empty_commit(tmp_path):
    root = _seed_patch_repo(tmp_path / "repo")
    agent = SelfImprovementAgent(root, ImprovementStore(tmp_path / "proposals"))
    plan = _patchable_plan(_git(root, "rev-parse", "HEAD"))
    plan.findings = []
    record = agent.prepare(plan)
    assert record.status == "no_changes"
    assert record.changed_files == []
    assert record.commit_sha is None


def test_validation_runs_candidate_sources_and_blocks_failing_tests(tmp_path, monkeypatch):
    worktree = tmp_path / "candidate"
    package = worktree / "evals/src/indusguard_evals"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text("def main(args):\n    assert args == ['validate']\n")
    (worktree / "evals/pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "--strict-markers"\n'
    )
    (worktree / "evals/tests").mkdir()
    (worktree / "apps/api/tests").mkdir(parents=True)
    test = worktree / "evals/tests/test_candidate.py"
    test.write_text(
        "import os\nimport pytest\n"
        "def test_environment():\n    assert 'GROQ_API_KEY' not in os.environ\n"
        "@pytest.mark.live\ndef test_live():\n    assert False\n"
        "@pytest.mark.postgres\ndef test_postgres():\n    assert False\n"
    )
    monkeypatch.setenv("GROQ_API_KEY", "must-not-reach-validation")
    log = tmp_path / "validation.log"
    assert SelfImprovementAgent._run_validation(worktree, log) is True
    test.write_text("def test_regression():\n    assert False\n")
    assert SelfImprovementAgent._run_validation(worktree, log) is False
    assert "test_regression" in log.read_text()
