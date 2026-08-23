"""Testes dos exportadores e da redaction que protegem os traces."""

import asyncio
import json
from pathlib import Path

import pytest
from conftest import REPOSITORY_ROOT

from indusguard_api.agent import (
    AgentDecision,
    AgentFinalAnswer,
    AgentIntentDecision,
    AgentPlanStep,
    ScriptedAgentModelGateway,
)
from indusguard_api.connectors import ConnectorCatalog
from indusguard_api.observability import NoOpTelemetry, OpenTelemetryRuntime
from indusguard_api.redaction import REDACTED_VALUE, redact_text, redact_value
from indusguard_api.runtime_factory import create_internal_agent_host
from indusguard_api.settings import Settings


def test_jsonl_exporter_preserves_trace_hierarchy_without_payloads(tmp_path: Path) -> None:
    trace_path = tmp_path / "traces.jsonl"
    telemetry = OpenTelemetryRuntime(
        service_name="indusguard-test",
        jsonl_path=trace_path,
    )

    with (
        telemetry.start_span("indusguard.agent.run", {"indusguard.run.id": "run-1"}),
        telemetry.start_span(
            "indusguard.model.plan",
            {"gen_ai.request.model": "scripted-test-model"},
        ),
    ):
        pass
    telemetry.shutdown()

    lines = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    by_name = {line["name"]: line for line in lines}
    root = by_name["indusguard.agent.run"]
    child = by_name["indusguard.model.plan"]
    assert root["trace_id"] == child["trace_id"]
    assert child["parent_span_id"] == root["span_id"]
    assert root["attributes"]["indusguard.run.id"] == "run-1"
    assert telemetry.snapshot().local_trace == "recorded"


def test_jsonl_failure_is_reported_without_raising(tmp_path: Path) -> None:
    file_instead_of_directory = tmp_path / "blocked"
    file_instead_of_directory.write_text("not a directory", encoding="utf-8")
    telemetry = OpenTelemetryRuntime(
        service_name="indusguard-test",
        jsonl_path=file_instead_of_directory / "traces.jsonl",
    )

    with telemetry.start_span("indusguard.agent.run"):
        pass

    assert telemetry.snapshot().degraded is True
    assert telemetry.snapshot().local_trace == "failed"
    telemetry.shutdown()


def test_noop_telemetry_never_creates_operational_dependency() -> None:
    telemetry = NoOpTelemetry()
    with telemetry.start_span("ignored"):
        pass

    assert telemetry.snapshot().enabled is False
    assert telemetry.force_flush() is True


def test_redaction_handles_nested_fields_lists_and_common_free_text() -> None:
    value = {
        "Authorization": "Bearer raw-credential",
        "nested": [
            {"internal_note": "não persistir"},
            "token=plain-secret",
            "Bearer another-secret",
        ],
    }

    redacted = redact_value(value, ["internal_note"], redact_strings=True)

    assert redacted == {
        "Authorization": REDACTED_VALUE,
        "nested": [
            {"internal_note": REDACTED_VALUE},
            f"token={REDACTED_VALUE}",
            f"Bearer {REDACTED_VALUE}",
        ],
    }
    assert redact_text("password:abc123") == f"password={REDACTED_VALUE}"


def test_otlp_requires_valid_header_pairs(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="chave=valor"):
        OpenTelemetryRuntime(
            service_name="indusguard-test",
            jsonl_path=tmp_path / "trace.jsonl",
            otlp_endpoint="https://telemetry.invalid/v1/traces",
            otlp_headers="invalid-header",
        )


def test_internal_host_composes_layers_and_closes_resources(tmp_path: Path) -> None:
    catalog = ConnectorCatalog(REPOSITORY_ROOT / "connectors")
    catalog.load()
    gateway = ScriptedAgentModelGateway(
        classification=AgentIntentDecision(intent_id="consultar"),
        plans=[AgentPlanStep(done=True)],
        final_answer=AgentFinalAnswer(answer="Pronto.", decision=AgentDecision.ORIENT),
    )
    settings = Settings(
        _env_file=None,
        connectors_dir=REPOSITORY_ROOT / "connectors",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'host.db'}",
        trace_jsonl_enabled=True,
        trace_jsonl_path=tmp_path / "host-traces.jsonl",
    )

    host = create_internal_agent_host(
        catalog=catalog,
        model_gateway=gateway,
        settings=settings,
        environment={"SYNTHETIC_API_URL": "http://localhost:9000"},
    )

    assert host.recorder is not None
    assert host.telemetry.snapshot().enabled is True
    asyncio.run(host.close())


def test_internal_host_can_disable_all_operational_sinks() -> None:
    catalog = ConnectorCatalog(REPOSITORY_ROOT / "connectors")
    catalog.load()
    gateway = ScriptedAgentModelGateway(
        classification=AgentIntentDecision(intent_id="consultar"),
        plans=[AgentPlanStep(done=True)],
        final_answer=AgentFinalAnswer(answer="Pronto.", decision=AgentDecision.ORIENT),
    )
    settings = Settings(
        _env_file=None,
        persist_runs=False,
        trace_jsonl_enabled=False,
        otlp_enabled=False,
    )

    host = create_internal_agent_host(
        catalog=catalog,
        model_gateway=gateway,
        settings=settings,
    )

    assert host.recorder is None
    assert host.telemetry.snapshot().enabled is False
    asyncio.run(host.close())


def test_telemetry_settings_require_endpoint_when_otlp_is_enabled() -> None:
    catalog = ConnectorCatalog(REPOSITORY_ROOT / "connectors")
    catalog.load()
    gateway = ScriptedAgentModelGateway(
        classification=AgentIntentDecision(intent_id="consultar"),
        plans=[AgentPlanStep(done=True)],
        final_answer=AgentFinalAnswer(answer="Pronto.", decision=AgentDecision.ORIENT),
    )
    settings = Settings(
        _env_file=None,
        persist_runs=False,
        trace_jsonl_enabled=False,
        otlp_enabled=True,
        otlp_endpoint=None,
    )

    with pytest.raises(ValueError, match="OTLP_ENDPOINT"):
        create_internal_agent_host(
            catalog=catalog,
            model_gateway=gateway,
            settings=settings,
        )
