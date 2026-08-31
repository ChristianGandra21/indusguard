"""Manifesto auditável que antecede qualquer cliente ou transmissão à Groq."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from indusguard_api.connectors import ConnectorCatalog, ConnectorValidationError
from indusguard_api.groq_gateway import GroqAgentSettings
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from indusguard_evals.contracts import EvaluationPhase, EvaluationVariant
from indusguard_evals.corpus import CorpusValidationError, OfficialCorpus
from indusguard_evals.pacing import GroqPilotPacingSettings
from indusguard_evals.schedule import build_schedule

PREFLIGHT_SCHEMA_VERSION = "groq-pilot-preflight-v2"
TRANSMITTED_CATEGORIES = [
    "ticket_message",
    "fixed_agent_prompts",
    "domain_and_tool_descriptions",
    "redacted_tool_results",
    "synthetic_evidence_ids",
]
EXCLUDED_CATEGORIES = [
    "goldens",
    "groq_api_key",
    "authentication_headers",
    "confirmation",
    "confirmation_digest",
    "unredacted_tool_payloads",
    "chain_of_thought",
]


class PreflightError(ValueError):
    """Erro estável de preparação que deve ser exibido pelo CLI sem traceback."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class PreflightRepository(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    git_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    worktree_clean: Literal[True]


class PreflightModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["groq"] = "groq"
    name: str
    timeout_seconds: float
    max_retries: int
    max_tokens: int
    temperature: Literal[0] = 0
    reasoning_effort: Literal["low"] = "low"
    minimum_request_interval_seconds: float = Field(ge=0, le=300)
    api_key_configured: Literal[True]


class PreflightCorpus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    connector_id: str
    pilot_scenarios: list[str]
    case_count: int


class PreflightScheduledRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    scenario_id: str
    variant: EvaluationVariant
    seed: int
    ordinal: int


class PreflightMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    scenario_id: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    utf8_bytes: int = Field(gt=0)


class PreflightTransmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    included_categories: list[str]
    excluded_categories: list[str]


class PreflightRuntimeBoundaries(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    upstream: Literal["local_asgi_fixture"] = "local_asgi_fixture"
    execution_mode: Literal["simulate"] = "simulate"
    real_writes_enabled: Literal[False] = False
    goldens_loaded: Literal[False] = False


class GroqPilotPreflightManifest(BaseModel):
    """Contrato sem payloads que vincula consentimento à execução planejada."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["groq-pilot-preflight-v2"] = PREFLIGHT_SCHEMA_VERSION
    created_at: datetime
    phase: Literal["pilot"] = "pilot"
    execution_kind: Literal["groq_pilot"] = "groq_pilot"
    repository: PreflightRepository
    model: PreflightModel
    corpus: PreflightCorpus
    schedule: list[PreflightScheduledRun]
    messages: list[PreflightMessage]
    transmission: PreflightTransmission
    runtime_boundaries: PreflightRuntimeBoundaries
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at precisa possuir timezone")
        return value.astimezone(UTC)


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _manifest_digest(value: dict[str, Any]) -> str:
    unsigned = {key: item for key, item in value.items() if key != "manifest_digest"}
    return hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def _repository(root: Path) -> PreflightRepository:
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        status = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PreflightError("PREFLIGHT_INVALID", "repositório Git indisponível") from exc
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise PreflightError("PREFLIGHT_INVALID", "commit Git desconhecido")
    if status.strip():
        raise PreflightError(
            "PREFLIGHT_DIRTY_WORKTREE",
            "o piloto exige um checkout sem alterações locais",
        )
    return PreflightRepository(git_commit=commit, worktree_clean=True)


def build_groq_pilot_preflight(
    root: Path,
    settings: GroqAgentSettings,
    pacing_settings: GroqPilotPacingSettings | None = None,
    *,
    created_at: datetime | None = None,
) -> GroqPilotPreflightManifest:
    """Valida somente fontes locais e não abre banco, golden, fixture HTTP ou cliente Groq."""

    if settings.api_key is None or not settings.api_key.get_secret_value().strip():
        raise PreflightError("MODEL_NOT_CONFIGURED", "GROQ_API_KEY precisa estar definida")
    pacing = pacing_settings or GroqPilotPacingSettings()
    repository = _repository(root)
    try:
        catalog = ConnectorCatalog(root / "connectors")
        catalog.load()
        inputs = OfficialCorpus(root / "evals" / "corpus" / "official-v1").load_inputs()
        schedule = build_schedule(inputs, EvaluationPhase.PILOT)
        scheduled = [
            PreflightScheduledRun.model_validate(item, from_attributes=True) for item in schedule
        ]
    except (
        OSError,
        ConnectorValidationError,
        CorpusValidationError,
        ValidationError,
        ValueError,
    ) as exc:
        raise PreflightError(
            "PREFLIGHT_INVALID",
            "catálogo, corpus ou agenda experimental inválido",
        ) from exc
    messages = [
        PreflightMessage(
            case_id=case.case_id,
            scenario_id=case.scenario_id,
            sha256=hashlib.sha256(case.message.encode("utf-8")).hexdigest(),
            utf8_bytes=len(case.message.encode("utf-8")),
        )
        for case in inputs.pilot_cases
    ]
    unsigned = {
        "schema_version": PREFLIGHT_SCHEMA_VERSION,
        "created_at": (created_at or datetime.now(UTC)).isoformat(),
        "phase": "pilot",
        "execution_kind": "groq_pilot",
        "repository": repository.model_dump(mode="json"),
        "model": PreflightModel(
            name=settings.model,
            timeout_seconds=settings.timeout_seconds,
            max_retries=settings.max_retries,
            max_tokens=settings.max_tokens,
            minimum_request_interval_seconds=pacing.minimum_interval_seconds,
            api_key_configured=True,
        ).model_dump(mode="json"),
        "corpus": PreflightCorpus(
            version=inputs.version,
            input_digest=inputs.digest,
            connector_id=inputs.connector_id,
            pilot_scenarios=inputs.pilot_scenarios,
            case_count=len(inputs.pilot_cases),
        ).model_dump(mode="json"),
        "schedule": [item.model_dump(mode="json") for item in scheduled],
        "messages": [item.model_dump(mode="json") for item in messages],
        "transmission": PreflightTransmission(
            included_categories=TRANSMITTED_CATEGORIES,
            excluded_categories=EXCLUDED_CATEGORIES,
        ).model_dump(mode="json"),
        "runtime_boundaries": PreflightRuntimeBoundaries().model_dump(mode="json"),
    }
    normalized = GroqPilotPreflightManifest.model_validate(
        {**unsigned, "manifest_digest": "0" * 64}
    ).model_dump(mode="json", exclude={"manifest_digest"})
    return GroqPilotPreflightManifest.model_validate(
        {**normalized, "manifest_digest": _manifest_digest(normalized)}
    )


def write_groq_pilot_preflight(
    root: Path,
    output: Path,
    settings: GroqAgentSettings,
    pacing_settings: GroqPilotPacingSettings | None = None,
) -> GroqPilotPreflightManifest:
    """Grava somente depois de todas as validações para não deixar artefato parcial válido."""

    manifest = build_groq_pilot_preflight(root, settings, pacing_settings)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return manifest


def load_and_validate_groq_pilot_preflight(
    root: Path,
    path: Path,
    settings: GroqAgentSettings,
    pacing_settings: GroqPilotPacingSettings | None = None,
) -> GroqPilotPreflightManifest:
    """Verifica contrato, integridade e igualdade com todas as fontes locais atuais."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise TypeError("o manifesto precisa conter um objeto JSON")
        manifest = GroqPilotPreflightManifest.model_validate(raw)
    except (OSError, json.JSONDecodeError, TypeError, ValidationError) as exc:
        raise PreflightError("PREFLIGHT_INVALID", "manifesto ausente ou malformado") from exc
    serialized = manifest.model_dump(mode="json")
    calculated = _manifest_digest(serialized)
    if not hmac.compare_digest(manifest.manifest_digest, calculated):
        raise PreflightError("PREFLIGHT_INVALID", "digest do manifesto não confere")
    expected = build_groq_pilot_preflight(
        root,
        settings,
        pacing_settings,
        created_at=manifest.created_at,
    )
    if not hmac.compare_digest(manifest.manifest_digest, expected.manifest_digest):
        raise PreflightError(
            "PREFLIGHT_STALE",
            "commit, corpus, modelo, agenda ou contrato de transmissão mudou",
        )
    return manifest


def require_persisted_preflight_digest(
    manifest: GroqPilotPreflightManifest,
    persisted_config: dict[str, Any],
) -> None:
    """Impede que uma retomada troque o consentimento que iniciou a avaliação."""

    persisted = persisted_config.get("preflight_manifest_digest")
    if not isinstance(persisted, str) or not hmac.compare_digest(
        persisted,
        manifest.manifest_digest,
    ):
        raise PreflightError(
            "PREFLIGHT_STALE",
            "a retomada exige o mesmo manifesto usado no início da avaliação",
        )
