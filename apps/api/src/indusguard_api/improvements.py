"""Contrato e armazenamento local do ciclo; API lê apenas a projeção administrativa."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

ImprovementStatus = Literal[
    "preparing",
    "prepared",
    "validating",
    "validation_failed",
    "pending_review",
    "no_changes",
    "failed",
    "rejected",
    "committing",
    "committed",
]


class ImprovementSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    evaluation_id: str
    status: ImprovementStatus
    created_at: datetime
    updated_at: datetime
    base_commit: str
    branch: str
    changed_files: list[str] = Field(default_factory=list)
    patch_digest: str | None = None
    validation_passed: bool = False
    approved_by: str | None = None
    approved_at: datetime | None = None
    commit_sha: str | None = None
    error_code: str | None = None


class ImprovementRecord(ImprovementSummary):
    """Detalhes locais nunca são devolvidos pelo endpoint administrativo."""

    worktree: str
    tree_sha: str | None = None
    plan: dict = Field(default_factory=dict)


class ImprovementStore:
    """Arquivos atômicos, em volume privado compartilhado entre CLI e API.

    O produtor serializa transições com flock. A API não importa nem executa o produtor.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def directory(self, proposal_id: str) -> Path:
        return self.root / str(UUID(proposal_id))

    def read(self, proposal_id: str) -> ImprovementRecord:
        return ImprovementRecord.model_validate_json(
            (self.directory(proposal_id) / "record.json").read_text(encoding="utf-8")
        )

    def save(self, record: ImprovementRecord) -> None:
        directory = self.directory(record.proposal_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        record.updated_at = datetime.now(UTC)
        temporary = directory / "record.tmp"
        temporary.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, directory / "record.json")

    def list_summaries(self) -> list[ImprovementSummary]:
        if not self.root.exists():
            return []
        records = []
        for path in self.root.glob("*/record.json"):
            # Corrupt records fail visibly; absence is never fabricated.
            record = ImprovementRecord.model_validate_json(path.read_text(encoding="utf-8"))
            records.append(
                ImprovementSummary.model_validate(
                    record.model_dump(include=set(ImprovementSummary.model_fields))
                )
            )
        return sorted(records, key=lambda item: item.created_at, reverse=True)
