"""CLI do benchmark; execuções externas permanecem manuais e explicitamente bloqueadas."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

from indusguard_api.agent import (
    AgentDecision,
    AgentFinalAnswer,
    AgentIntentDecision,
    AgentPlanStep,
    ScriptedAgentModelGateway,
)
from indusguard_api.connectors import ConnectorCatalog
from indusguard_api.executor import HttpExecutor
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
from indusguard_evals.repository import EvaluationRepository
from indusguard_evals.runner import BenchmarkRunner
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


async def _offline_runner(
    root: Path,
    database_url: str,
) -> tuple[BenchmarkRunner, Any, Any]:
    """Monta as duas variantes com fixture ASGI; nenhuma chamada sai da máquina."""

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
            model_gateway=_fake_gateway(),
            recorder=recorder,
        ),
        EvaluationVariant.PROMPT_ONLY: create_variant_runtime(
            variant=EvaluationVariant.PROMPT_ONLY,
            catalog=catalog,
            executor=PromptOnlyExecutor(catalog, baseline_http),
            shadow_policy=shadow,
            model_gateway=_fake_gateway(),
            recorder=recorder,
        ),
    }
    return (
        BenchmarkRunner(
            corpus=_corpus(root),
            catalog=catalog,
            repository=repository,
            runtimes=runtimes,
        ),
        client,
        engine,
    )


async def _run_offline(args: argparse.Namespace, phase: EvaluationPhase) -> int:
    if not args.fake:
        raise SystemExit(
            "Execução Groq pausada: tickets, contexto e evidências seriam enviados a um provedor "
            "externo. Use --fake para smoke local até haver autorização explícita documentada."
        )
    root = _repository_root()
    runner, client, engine = await _offline_runner(root, args.database_url)
    try:
        evaluation_id = await runner.start(
            phase=phase,
            model="scripted-eval-smoke",
            git_commit=_git_commit(root),
            execution_kind=EvaluationExecutionKind.OFFLINE_SMOKE,
        )
        summary = await runner.execute(evaluation_id)
    finally:
        await client.aclose()
        await engine.dispose()
    print(evaluation_id)
    print(summary.model_dump_json(indent=2))
    return 0


async def _resume_offline(args: argparse.Namespace) -> int:
    if not args.fake:
        raise SystemExit("Resume Groq permanece bloqueado; use --fake somente para smoke local.")
    root = _repository_root()
    runner, client, engine = await _offline_runner(root, args.database_url)
    try:
        summary = await runner.execute(args.evaluation_id)
    finally:
        await client.aclose()
        await engine.dispose()
    print(summary.model_dump_json(indent=2))
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
    for command in ("pilot", "run"):
        child = subparsers.add_parser(command)
        child.add_argument("--fake", action="store_true", help="smoke local, sem valor científico")
    resume = subparsers.add_parser("resume")
    resume.add_argument("evaluation_id")
    resume.add_argument("--fake", action="store_true")
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
    if args.command == "pilot":
        return asyncio.run(_run_offline(args, EvaluationPhase.PILOT))
    if args.command == "run":
        return asyncio.run(_run_offline(args, EvaluationPhase.FULL))
    if args.command == "resume":
        return asyncio.run(_resume_offline(args))
    if args.command == "report":
        return asyncio.run(_report(args))
    if args.command == "review":
        return asyncio.run(_review(args))
    raise AssertionError("comando não tratado")
