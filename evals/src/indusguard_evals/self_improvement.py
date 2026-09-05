"""Agente local de receitas: prepara, valida e promove somente após revisão no terminal.

Não há cliente de GitHub, push, PR, modelo externo ou comandos oriundos dos resultados de eval.
O acesso de escrita ao volume e ao repositório pertence ao operador confiável.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from indusguard_api.improvements import ImprovementRecord, ImprovementStore

from indusguard_evals.analysis import ImprovementPlan
from indusguard_evals.improvement import ImprovementPatchWriter


class SelfImprovementError(ValueError):
    """Falha de transição com código estável, sem detalhes do ambiente."""


class SelfImprovementAgent:
    def __init__(self, root: Path, store: ImprovementStore) -> None:
        self.root = root.resolve()
        self.store = store

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.store.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.store.root / ".lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def prepare(self, plan: ImprovementPlan) -> ImprovementRecord:
        """Recebe exclusivamente o plano retornado pelo EvaluationAnalyzer no CLI."""
        with self._lock():
            proposal_id = str(uuid4())
            directory = self.store.directory(proposal_id)
            worktree = directory / "worktree"
            branch = f"improvement/{proposal_id}"
            now = datetime.now(UTC)
            record = ImprovementRecord(
                proposal_id=proposal_id,
                evaluation_id=plan.evaluation_id,
                status="preparing",
                created_at=now,
                updated_at=now,
                base_commit=plan.git_commit,
                branch=branch,
                worktree=str(worktree),
                plan=plan.model_dump(mode="json"),
            )
            self.store.save(record)
            try:
                self._git(
                    self.root, "worktree", "add", "-b", branch, str(worktree), plan.git_commit
                )
                # Mudar a baseline para melhorar o contraste não é uma melhoria do agente.
                agent_plan = plan.model_copy(update={"benchmark_criteria": {}})
                result = ImprovementPatchWriter(worktree).apply(agent_plan)
                record.changed_files = result.changed_files
                if record.changed_files:
                    patch = self._git(
                        worktree, "diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD"
                    )
                    (directory / "patch.diff").write_text(patch, encoding="utf-8")
                    record.patch_digest = hashlib.sha256(patch.encode()).hexdigest()
                    self._git(worktree, "add", "--", *record.changed_files)
                    record.tree_sha = self._git(worktree, "write-tree").strip()
                    record.status = "prepared"
                else:
                    record.status = "no_changes"
            except Exception:
                record.status = "failed"
                record.error_code = "IMPROVEMENT_PREPARATION_FAILED"
                self.store.save(record)
                raise SelfImprovementError(record.error_code) from None
            self.store.save(record)
            return record

    def validate(self, proposal_id: str) -> ImprovementRecord:
        with self._lock():
            record = self.store.read(proposal_id)
            if record.status not in {
                "prepared",
                "validation_failed",
                "validating",
                "pending_review",
            }:
                raise SelfImprovementError("IMPROVEMENT_INVALID_STATE")
            self._verify_snapshot(record)
            record.status = "validating"
            record.validation_passed = False
            record.error_code = None
            self.store.save(record)
            directory = self.store.directory(proposal_id)
            try:
                passed = self._run_validation(Path(record.worktree), directory / "validation.log")
                self._verify_snapshot(record)
                record.validation_passed = passed
                record.status = "pending_review" if passed else "validation_failed"
                record.error_code = None if passed else "IMPROVEMENT_VALIDATION_FAILED"
            except Exception:
                record.status = "validation_failed"
                record.error_code = "IMPROVEMENT_VALIDATION_FAILED"
            self.store.save(record)
            return record

    def review(
        self,
        proposal_id: str,
        *,
        confirm: Callable[[str], str],
        interactive: bool,
    ) -> ImprovementRecord:
        """Confirmação local obrigatória; não existe flag de autoaprovação ou rota HTTP."""
        if not interactive:
            raise SelfImprovementError("IMPROVEMENT_HUMAN_TERMINAL_REQUIRED")
        with self._lock():
            record = self.store.read(proposal_id)
            if record.status != "pending_review" or not record.validation_passed:
                raise SelfImprovementError("IMPROVEMENT_REVIEW_NOT_READY")
            self._verify_snapshot(record)
            patch = self._git(
                Path(record.worktree), "diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD"
            )
            answer = confirm(
                f"Proposta: {record.proposal_id}\nBase: {record.base_commit}\n"
                f"Branch local: {record.branch}\n\n{patch}\n"
                f"Para aprovar ESTE diff e criar o commit, digite {record.patch_digest}\n"
                "Digite rejeitar para rejeitar; qualquer outra entrada cancela: "
            )
            if answer == "rejeitar":
                record.status = "rejected"
                self.store.save(record)
                return record
            if answer != record.patch_digest:
                raise SelfImprovementError("IMPROVEMENT_APPROVAL_CANCELLED")
            self._verify_snapshot(record)
            # Git identity identifica o operador local, não uma identidade web autenticada.
            record.approved_by = self._git(self.root, "var", "GIT_COMMITTER_IDENT").strip()
            record.approved_at = datetime.now(UTC)
            record.commit_sha = self._git(
                self.root,
                "commit-tree",
                str(record.tree_sha),
                "-p",
                record.base_commit,
                "-m",
                f"Improve agent guidance ({record.proposal_id})\n\n"
                f"Evaluation: {record.evaluation_id}\nApproved-patch: {record.patch_digest}",
            ).strip()
            record.status = "committing"
            # Save intent before CAS; recovery can finish without creating another commit.
            self.store.save(record)
            return self._finish_commit(record)

    def recover(self, proposal_id: str) -> ImprovementRecord:
        with self._lock():
            record = self.store.read(proposal_id)
            if record.status != "committing" or not record.approved_at or not record.commit_sha:
                raise SelfImprovementError("IMPROVEMENT_INVALID_STATE")
            return self._finish_commit(record)

    def _finish_commit(self, record: ImprovementRecord) -> ImprovementRecord:
        ref = f"refs/heads/{record.branch}"
        current = self._git(self.root, "rev-parse", ref).strip()
        if current != record.commit_sha:
            self._git(self.root, "update-ref", ref, str(record.commit_sha), record.base_commit)
        record.status = "committed"
        self.store.save(record)
        return record

    def _verify_snapshot(self, record: ImprovementRecord) -> None:
        worktree = Path(record.worktree)
        expected = self.store.directory(record.proposal_id) / "worktree"
        if worktree.resolve() != expected.resolve():
            raise SelfImprovementError("IMPROVEMENT_WORKTREE_MISMATCH")
        if self._git(worktree, "rev-parse", "HEAD").strip() != record.base_commit:
            raise SelfImprovementError("IMPROVEMENT_BASE_CHANGED")
        if self._git(worktree, "symbolic-ref", "HEAD").strip() != f"refs/heads/{record.branch}":
            raise SelfImprovementError("IMPROVEMENT_BRANCH_CHANGED")
        patch = self._git(worktree, "diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD")
        if hashlib.sha256(patch.encode()).hexdigest() != record.patch_digest:
            raise SelfImprovementError("IMPROVEMENT_PATCH_CHANGED")
        if self._git(worktree, "write-tree").strip() != record.tree_sha:
            raise SelfImprovementError("IMPROVEMENT_INDEX_CHANGED")
        if self._git(worktree, "ls-files", "--others", "--exclude-standard").strip():
            raise SelfImprovementError("IMPROVEMENT_UNTRACKED_FILES")

    @staticmethod
    def _run_validation(worktree: Path, log: Path) -> bool:
        # O interpretador é do operador; imports e testes são os do candidato isolado.
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT"}
        }
        environment["PYTHONPATH"] = os.pathsep.join(
            [
                str(worktree / "apps/api/src"),
                str(worktree / "evals/src"),
            ]
        )
        environment["INDUSGUARD_DATABASE_URL"] = f"sqlite+aiosqlite:///{worktree}/.data/check.db"
        (worktree / ".data").mkdir(exist_ok=True)
        commands = [
            [sys.executable, "-c", "from indusguard_evals.cli import main; main(['validate'])"],
            [
                sys.executable,
                "-m",
                "pytest",
                "-c",
                "evals/pyproject.toml",
                "evals/tests",
                "apps/api/tests",
                "-q",
                "-m",
                "not live and not postgres",
                "-o",
                "markers=live: external provider\npostgres: external database",
            ],
        ]
        with log.open("w", encoding="utf-8") as output:
            for command in commands:
                result = subprocess.run(
                    command,
                    cwd=worktree,
                    env=environment,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    timeout=600,
                    check=False,
                )
                if result.returncode:
                    return False
        return True

    @staticmethod
    def _git(root: Path, *args: str) -> str:
        try:
            return subprocess.check_output(
                ["git", *args],
                cwd=root,
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError):
            raise SelfImprovementError("IMPROVEMENT_GIT_FAILED") from None
