"""CLI do benchmark com consentimento explícito para o piloto Groq autorizado."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from typing import Any

from indusguard_api.agent import (
    AgentConfigurationError,
    AgentDecision,
    AgentFinalAnswer,
    AgentIntentDecision,
    AgentModelGateway,
    AgentPlanStep,
    ScriptedAgentModelGateway,
)
from indusguard_api.connectors import ConnectorCatalog
from indusguard_api.executor import HttpExecutor
from indusguard_api.groq_gateway import GroqAgentModelGateway, GroqAgentSettings
from indusguard_api.persistence import SqlAlchemyAgentRunRecorder, normalize_database_url
from indusguard_api.policy import GuardedExecutor, PolicyEngine
from indusguard_api.settings import Settings
from sqlalchemy.ext.asyncio import create_async_engine

from indusguard_evals.baseline import PromptOnlyExecutor
from indusguard_evals.contracts import (
    EvaluationExecutionKind,
    EvaluationPhase,
    EvaluationVariant,
)
from indusguard_evals.corpus import OfficialCorpus
from indusguard_evals.execution import create_variant_runtime
from indusguard_evals.human_review import export_human_review
from indusguard_evals.pacing import GroqPilotPacingSettings, PacedAgentModelGateway
from indusguard_evals.preflight import (
    GroqPilotPreflightManifest,
    PreflightError,
    load_and_validate_groq_pilot_preflight,
    require_persisted_preflight_digest,
    write_groq_pilot_preflight,
)
from indusguard_evals.report import BenchmarkSummary
from indusguard_evals.repository import EvaluationRepository
from indusguard_evals.runner import BenchmarkRunner, EvaluationProgress
from indusguard_evals.tractian_fixture import store


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _corpus(root: Path) -> OfficialCorpus:
    return OfficialCorpus(root / "evals" / "corpus" / "official-v1")


def _git_commit(root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _fake_gateway() -> ScriptedAgentModelGateway:
    """Smoke offline: exercita o pipeline, mas seus scores não são resultado científico."""

    return ScriptedAgentModelGateway(
        classification=AgentIntentDecision(intent_id="investigar"),
        plans=[AgentPlanStep(done=True)],
        final_answer=AgentFinalAnswer(
            answer="Smoke offline concluído sem coletar evidências externas.",
            decision=AgentDecision.ORIENT,
            evidence_ids=[],
            uncertainties=["OFFLINE_FAKE_NOT_A_BENCHMARK_RESULT"],
        ),
        model_name="scripted-eval-smoke",
    )


def _print_progress(progress: EvaluationProgress) -> None:
    """Mantém eventos incrementais fora do stdout reservado ao resultado final."""

    print(progress.model_dump_json(), file=sys.stderr, flush=True)


def _enforce_resume_window(
    summary_payload: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> None:
    """Evita construir gateway enquanto o Retry-After persistido ainda está ativo."""

    if summary_payload is None:
        return
    try:
        summary = BenchmarkSummary.model_validate(summary_payload)
    except ValueError:
        # Resumos legados ou incompletos não ganham uma restrição que não registraram.
        return
    interruption = summary.interruption
    if interruption is None or interruption.resume_not_before is None:
        return
    current = now or datetime.now(UTC)
    resume_at = interruption.resume_not_before.astimezone(UTC)
    if current >= resume_at:
        return
    remaining = ceil((resume_at - current).total_seconds())
    timestamp = resume_at.isoformat().replace("+00:00", "Z")
    raise SystemExit(
        f"MODEL_RATE_LIMITED: retomada disponível após {timestamp} ({remaining} segundos restantes)"
    )


def _rate_limit_guidance(summary: BenchmarkSummary) -> str:
    interruption = summary.interruption
    if interruption is None:
        return "A avaliação ficou parcial; use resume para continuar os checkpoints pendentes."
    if interruption.resume_not_before is None:
        return (
            "MODEL_RATE_LIMITED: a Groq não informou Retry-After; "
            "não é possível indicar um horário seguro de retomada."
        )
    timestamp = interruption.resume_not_before.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return (
        "MODEL_RATE_LIMITED: Retry-After="
        f"{interruption.retry_after_seconds}s; retome a partir de {timestamp}."
    )


async def _runner(
    root: Path,
    database_url: str,
    model_gateway: AgentModelGateway,
) -> tuple[BenchmarkRunner, Any, Any]:
    """Monta variantes equivalentes; somente o gateway decide se haverá tráfego à Groq."""

    import httpx

    from indusguard_evals.tractian_fixture.main import app

    store.configure_data_dir(root / "evals" / "corpus" / "official-v1" / "fixture" / "data")
    catalog = ConnectorCatalog(root / "connectors")
    catalog.load()
    engine = create_async_engine(normalize_database_url(database_url))
    repository = EvaluationRepository(engine)
    recorder = SqlAlchemyAgentRunRecorder(engine)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost:8000",
    )
    environment = {"TRACTIAN_API_URL": "http://localhost:8000"}
    guarded_http = HttpExecutor(
        catalog,
        client=client,
        execution_mode="simulate",
        environment=environment,
    )
    baseline_http = HttpExecutor(
        catalog,
        client=client,
        execution_mode="simulate",
        environment=environment,
    )
    shadow = PolicyEngine(catalog, execution_mode="simulate")
    runtimes = {
        EvaluationVariant.GUARDED: create_variant_runtime(
            variant=EvaluationVariant.GUARDED,
            catalog=catalog,
            executor=GuardedExecutor(shadow, guarded_http),
            shadow_policy=shadow,
            model_gateway=model_gateway,
            recorder=recorder,
        ),
        EvaluationVariant.PROMPT_ONLY: create_variant_runtime(
            variant=EvaluationVariant.PROMPT_ONLY,
            catalog=catalog,
            executor=PromptOnlyExecutor(catalog, baseline_http),
            shadow_policy=shadow,
            model_gateway=model_gateway,
            recorder=recorder,
        ),
    }
    return (
        BenchmarkRunner(
            corpus=_corpus(root),
            catalog=catalog,
            repository=repository,
            runtimes=runtimes,
            on_progress=_print_progress,
        ),
        client,
        engine,
    )


def _requested_execution_kind(
    args: argparse.Namespace,
    *,
    command: str,
) -> EvaluationExecutionKind:
    """Valida autorização antes de ler chave ou construir qualquer cliente externo."""

    if args.fake and args.groq:
        raise SystemExit("EVALUATION_MODE_CONFLICT: escolha somente --fake ou --groq")
    if args.groq:
        if command == "run":
            raise SystemExit(
                "FULL_BENCHMARK_NOT_AUTHORIZED: somente o piloto CEN-01/CEN-14 pode usar Groq"
            )
        if not args.confirm_external_transmission:
            raise SystemExit(
                "EXTERNAL_TRANSMISSION_CONSENT_REQUIRED: acrescente "
                "--confirm-external-transmission para autorizar o envio à Groq"
            )
        return EvaluationExecutionKind.GROQ_PILOT
    if args.confirm_external_transmission:
        raise SystemExit(
            "EXTERNAL_TRANSMISSION_MODE_REQUIRED: o consentimento só é válido junto de --groq"
        )
    if args.fake:
        return EvaluationExecutionKind.OFFLINE_SMOKE
    raise SystemExit("EVALUATION_MODE_REQUIRED: escolha --fake ou --groq")


def _validated_preflight(
    args: argparse.Namespace,
    kind: EvaluationExecutionKind,
    root: Path,
) -> tuple[GroqAgentSettings | None, GroqPilotPreflightManifest | None]:
    path = args.preflight_manifest
    if kind is EvaluationExecutionKind.OFFLINE_SMOKE:
        if path is not None:
            raise SystemExit(
                "PREFLIGHT_MODE_MISMATCH: manifesto do piloto Groq não pode ser usado com --fake"
            )
        return None, None
    if path is None:
        raise SystemExit(
            "PILOT_PREFLIGHT_REQUIRED: informe --preflight-manifest para autorizar esta execução"
        )
    settings = GroqAgentSettings()
    try:
        return settings, load_and_validate_groq_pilot_preflight(root, path, settings)
    except PreflightError as exc:
        raise SystemExit(str(exc)) from exc


def _gateway(
    kind: EvaluationExecutionKind,
    groq_settings: GroqAgentSettings | None = None,
) -> AgentModelGateway:
    if kind is EvaluationExecutionKind.OFFLINE_SMOKE:
        return _fake_gateway()
    try:
        gateway = GroqAgentModelGateway(groq_settings or GroqAgentSettings())
        pacing = GroqPilotPacingSettings()
        return PacedAgentModelGateway(
            gateway,
            minimum_interval_seconds=pacing.minimum_interval_seconds,
        )
    except AgentConfigurationError as exc:
        raise SystemExit(f"MODEL_NOT_CONFIGURED: {exc}") from exc


async def _run_phase(args: argparse.Namespace, phase: EvaluationPhase) -> int:
    kind = _requested_execution_kind(args, command=args.command)
    root = _repository_root()
    groq_settings, manifest = _validated_preflight(args, kind, root)
    gateway = _gateway(kind, groq_settings)
    runner, client, engine = await _runner(root, args.database_url, gateway)
    try:
        evaluation_id = await runner.start(
            phase=phase,
            model=gateway.model_name,
            git_commit=_git_commit(root),
            execution_kind=kind,
            preflight_manifest_digest=(manifest.manifest_digest if manifest else None),
        )
        summary = await runner.execute(evaluation_id)
    finally:
        await client.aclose()
        await engine.dispose()
    print(evaluation_id)
    print(summary.model_dump_json(indent=2))
    if kind is EvaluationExecutionKind.GROQ_PILOT and summary.status == "partial":
        print(_rate_limit_guidance(summary))
        print(
            "Retome sem duplicar runs concluídas: "
            f"indusguard-eval resume {evaluation_id} --groq "
            "--confirm-external-transmission "
            f"--preflight-manifest {args.preflight_manifest}"
        )
    return 0


async def _resume(args: argparse.Namespace) -> int:
    kind = _requested_execution_kind(args, command=args.command)
    root = _repository_root()
    groq_settings, manifest = _validated_preflight(args, kind, root)
    inspection_engine = create_async_engine(normalize_database_url(args.database_url))
    try:
        persisted = await EvaluationRepository(inspection_engine).get(args.evaluation_id)
    finally:
        await inspection_engine.dispose()
    if persisted is None:
        raise SystemExit(f"EVALUATION_NOT_FOUND: {args.evaluation_id}")
    persisted_kind = persisted.config.get("execution_kind", EvaluationExecutionKind.UNKNOWN.value)
    if persisted_kind != kind.value:
        raise SystemExit(
            f"EVALUATION_MODE_MISMATCH: use o mesmo modo que iniciou a avaliação ({persisted_kind})"
        )
    if kind is EvaluationExecutionKind.GROQ_PILOT and persisted.phase is not EvaluationPhase.PILOT:
        raise SystemExit("FULL_BENCHMARK_NOT_AUTHORIZED: uma avaliação full não pode usar Groq")
    if manifest is not None:
        try:
            require_persisted_preflight_digest(manifest, persisted.config)
        except PreflightError as exc:
            raise SystemExit(str(exc)) from exc
    if kind is EvaluationExecutionKind.GROQ_PILOT:
        _enforce_resume_window(persisted.summary)

    gateway = _gateway(kind, groq_settings)
    runner, client, engine = await _runner(root, args.database_url, gateway)
    try:
        summary = await runner.execute(args.evaluation_id)
    finally:
        await client.aclose()
        await engine.dispose()
    print(summary.model_dump_json(indent=2))
    if kind is EvaluationExecutionKind.GROQ_PILOT and summary.status == "partial":
        print(_rate_limit_guidance(summary))
        print(
            "Checkpoints concluídos não serão duplicados. "
            f"Manifesto autorizado: {args.preflight_manifest}."
        )
    return 0


async def _report(args: argparse.Namespace) -> int:
    engine = create_async_engine(normalize_database_url(args.database_url))
    repository = EvaluationRepository(engine)
    try:
        run = await repository.get(args.evaluation_id)
    finally:
        await engine.dispose()
    if run is None:
        raise SystemExit(f"avaliação não encontrada: {args.evaluation_id}")
    payload = json.dumps(run.summary or {}, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


async def _review(args: argparse.Namespace) -> int:
    root = _repository_root()
    engine = create_async_engine(normalize_database_url(args.database_url))
    repository = EvaluationRepository(engine)
    try:
        samples = await repository.samples(args.evaluation_id)
    finally:
        await engine.dispose()
    inputs = _corpus(root).load_inputs()
    key_path = export_human_review(samples, inputs, args.output)
    print(f"CSV cegado: {args.output}")
    print(f"Chave separada: {key_path}")
    return 0


def _parser() -> argparse.ArgumentParser:
    settings = Settings()
    parser = argparse.ArgumentParser(prog="indusguard-eval")
    parser.add_argument("--database-url", default=settings.database_url)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="valida entradas, golden e digests")
    preflight = subparsers.add_parser("preflight", help="gera manifesto local do piloto Groq")
    preflight.add_argument("--groq", action="store_true", help="prepara o piloto Groq autorizado")
    preflight.add_argument("--output", type=Path, required=True)
    for command in ("pilot", "run"):
        child = subparsers.add_parser(command)
        child.add_argument("--fake", action="store_true", help="smoke local, sem valor científico")
        child.add_argument("--groq", action="store_true", help="usa a Groq Free")
        child.add_argument(
            "--confirm-external-transmission",
            action="store_true",
            help="confirma o envio de entradas e evidências à Groq",
        )
        child.add_argument("--preflight-manifest", type=Path)
    resume = subparsers.add_parser("resume")
    resume.add_argument("evaluation_id")
    resume.add_argument("--fake", action="store_true")
    resume.add_argument("--groq", action="store_true")
    resume.add_argument("--confirm-external-transmission", action="store_true")
    resume.add_argument("--preflight-manifest", type=Path)
    report = subparsers.add_parser("report")
    report.add_argument("evaluation_id")
    report.add_argument("--output", type=Path)
    review = subparsers.add_parser("review")
    review.add_argument("evaluation_id")
    review.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "validate":
        corpus = _corpus(_repository_root())
        inputs = corpus.load_inputs()
        goldens = corpus.load_goldens(inputs)
        print(
            f"{inputs.version}: {len(inputs.cases)} tickets, "
            f"{len({case.scenario_id for case in inputs.cases})} cenários, "
            f"inputs={inputs.digest}, goldens={goldens.digest}"
        )
        return 0
    if args.command == "preflight":
        if not args.groq:
            raise SystemExit("PREFLIGHT_MODE_MISMATCH: o preflight disponível exige --groq")
        try:
            manifest = write_groq_pilot_preflight(
                _repository_root(),
                args.output,
                GroqAgentSettings(),
            )
        except PreflightError as exc:
            raise SystemExit(str(exc)) from exc
        print(args.output)
        print(manifest.manifest_digest)
        return 0
    if args.command == "pilot":
        return asyncio.run(_run_phase(args, EvaluationPhase.PILOT))
    if args.command == "run":
        return asyncio.run(_run_phase(args, EvaluationPhase.FULL))
    if args.command == "resume":
        return asyncio.run(_resume(args))
    if args.command == "report":
        return asyncio.run(_report(args))
    if args.command == "review":
        return asyncio.run(_review(args))
    raise AssertionError("comando não tratado")
