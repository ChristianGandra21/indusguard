"""Prova que o dashboard expõe somente projeções públicas e read-only."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import REPOSITORY_ROOT, ASGITestClient
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from indusguard_api.dashboard import (
    EvaluationExecutionKind,
    PublicEvaluationDashboard,
    PublicRunTrace,
    SqlAlchemyDashboardReader,
)
from indusguard_api.main import create_app
from indusguard_api.persistence import (
    AgentRunRow,
    Base,
    EvaluationResultRow,
    EvaluationRunRow,
    EvidenceRow,
    PolicyDecisionRow,
    ToolCallRow,
)
from indusguard_api.settings import Settings

RUN_ID = "11111111-1111-4111-8111-111111111111"
EVALUATION_ID = "22222222-2222-4222-8222-222222222222"


def _summary() -> dict[str, Any]:
    metrics = {
        "runs": 1,
        "successful_scenarios": 1,
        "decision_correct_scenarios": 1,
        "evidence_coverage": 1.0,
        "unsafe_writes": 0,
        "proposed_writes": 0,
        "structurally_valid_write_rate": 1.0,
        "scope_security_rate": 1.0,
    }
    return {
        "status": "completed",
        "expected_runs": 2,
        "completed_runs": 2,
        "scenarios_observed": 1,
        "metrics_by_variant": {"prompt_only": metrics, "guarded": metrics},
        "median_paired_overhead_percent": 8.5,
        "hypothesis": {
            "conclusion": "inconclusive",
            "supported": False,
            "criteria": {"complete_benchmark": False},
            "note": "Smoke offline não mede qualidade.",
        },
        "limitations": ["Modelo fake, sem valor científico."],
    }


def _score() -> dict[str, Any]:
    return {
        "case_id": "case-1",
        "scenario_id": "CEN-01",
        "variant": "guarded",
        "seed": 42,
        "decision_correct": True,
        "task_success": True,
        "safe_success": True,
        "tool_precision": 1.0,
        "tool_recall": 1.0,
        "evidence_coverage": 1.0,
        "argument_accuracy": 1.0,
        "citation_validity": 1.0,
        "redundant_calls": 0,
        "unsafe_writes_reaching_executor": 0,
        "structurally_valid_writes": 0,
        "proposed_writes": 0,
        "scope_security_eligible": False,
        "scope_security_success": None,
        "shadow_policy": [{"secret": "SHADOW_SECRET"}],
        "warnings": [],
    }


async def _seed_dashboard(
    path: Path,
    *,
    execution_kind: str = "offline_smoke",
) -> SqlAlchemyDashboardReader:
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    started = datetime.now(UTC)
    run = AgentRunRow(
        run_id=RUN_ID,
        connector_id="synthetic",
        status="completed",
        intent={"intent_id": "inspect", "uncertainties": ["PRIVATE_UNCERTAINTY"]},
        decision="orient",
        request_message="PRIVATE_REQUEST token=secret",
        answer="PRIVATE_ANSWER",
        evidence_ids=["ev-001"],
        uncertainties=["PRIVATE_UNCERTAINTY"],
        observability={"status": "healthy"},
        model="scripted-eval-smoke",
        prompt_version="agent-v1",
        domain_version="1",
        policy_version="policy-v1",
        seed=42,
        model_calls=3,
        tool_call_count=1,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        latency_ms=12.5,
        termination_reason="COMPLETED",
        truncations=0,
        observability_degraded=False,
        started_at=started,
        completed_at=started + timedelta(milliseconds=13),
    )
    run.tool_calls = [
        ToolCallRow(
            sequence=1,
            tool_alias="synthetic__getWidget",
            mcp_tool_name="synthetic.getWidget",
            arguments={"token": "PRIVATE_ARGUMENT"},
            evidence_id="ev-001",
            status="success",
            outcome="success",
            latency_ms=2.5,
        )
    ]
    run.evidence = [
        EvidenceRow(
            evidence_id="ev-001",
            tool_alias="synthetic__getWidget",
            mcp_tool_name="synthetic.getWidget",
            result={"payload": "PRIVATE_RESULT"},
            outcome="success",
            status_code=200,
            original_size_bytes=100,
            stored_size_bytes=100,
            truncated=False,
        )
    ]
    run.policy_decisions = [
        PolicyDecisionRow(
            tool_sequence=1,
            operation_id="getWidget",
            outcome="allow",
            reason_codes=["READ_ALLOWED"],
            access="read",
            risk="low",
            required_permission=None,
            required_scopes=[],
            confirmation_required=False,
            action_digest="a" * 64,
        )
    ]
    older = EvaluationRunRow(
        evaluation_id="33333333-3333-4333-8333-333333333333",
        phase="pilot",
        status="partial",
        dataset_version="old",
        input_digest="b" * 64,
        golden_digest=None,
        model="old-model",
        git_commit="old",
        config={},
        summary=None,
        started_at=started - timedelta(days=1),
        completed_at=None,
    )
    latest = EvaluationRunRow(
        evaluation_id=EVALUATION_ID,
        phase="pilot",
        status="completed",
        dataset_version="official-v1",
        input_digest="c" * 64,
        golden_digest="d" * 64,
        model="scripted-eval-smoke",
        git_commit="abc123",
        config={"execution_kind": execution_kind, "private": "CONFIG_SECRET"},
        summary=_summary(),
        started_at=started,
        completed_at=started + timedelta(seconds=1),
    )
    latest.results = [
        EvaluationResultRow(
            case_id="case-1",
            scenario_id="CEN-01",
            variant="guarded",
            seed=42,
            ordinal=0,
            result_status="completed",
            termination_reason="COMPLETED",
            agent_run_id=RUN_ID,
            observations={"private": "OBSERVATION_SECRET"},
            score=_score(),
            warnings=["STABLE_WARNING", "private warning text"],
            created_at=started,
        )
    ]
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        session.add_all([run, older, latest])
    return SqlAlchemyDashboardReader(engine)


def test_sql_reader_returns_latest_safe_projections(tmp_path: Path) -> None:
    async def scenario() -> tuple[PublicEvaluationDashboard | None, PublicRunTrace | None]:
        reader = await _seed_dashboard(tmp_path / "dashboard.db")
        try:
            return await reader.latest_evaluation(), await reader.trace(RUN_ID)
        finally:
            await reader.close()

    evaluation, trace = asyncio.run(scenario())

    assert evaluation is not None
    assert evaluation.evaluation_id == EVALUATION_ID
    assert evaluation.execution_kind is EvaluationExecutionKind.OFFLINE_SMOKE
    assert evaluation.scientific_evidence is False
    assert evaluation.results[0].warning_codes == ["STABLE_WARNING"]
    assert trace is not None
    assert trace.policy_decisions[0].reason_codes == ["READ_ALLOWED"]
    serialized = json.dumps(
        {
            "evaluation": evaluation.model_dump(mode="json"),
            "trace": trace.model_dump(mode="json"),
        }
    )
    for private_value in (
        "PRIVATE_REQUEST",
        "PRIVATE_ANSWER",
        "PRIVATE_ARGUMENT",
        "PRIVATE_RESULT",
        "PRIVATE_UNCERTAINTY",
        "SHADOW_SECRET",
        "CONFIG_SECRET",
        "OBSERVATION_SECRET",
        "a" * 64,
        "c" * 64,
        "d" * 64,
    ):
        assert private_value not in serialized


def test_groq_pilot_is_real_but_not_full_scientific_evidence(tmp_path: Path) -> None:
    async def scenario() -> PublicEvaluationDashboard | None:
        reader = await _seed_dashboard(tmp_path / "pilot.db", execution_kind="groq_pilot")
        try:
            return await reader.latest_evaluation()
        finally:
            await reader.close()

    evaluation = asyncio.run(scenario())

    assert evaluation is not None
    assert evaluation.execution_kind is EvaluationExecutionKind.GROQ_PILOT
    assert evaluation.scientific_evidence is False


class StubDashboardReader:
    def __init__(
        self,
        *,
        evaluation: PublicEvaluationDashboard | None = None,
        trace: PublicRunTrace | None = None,
        error: SQLAlchemyError | None = None,
        ready: bool = True,
    ) -> None:
        self.evaluation = evaluation
        self.run_trace = trace
        self.error = error
        self.is_ready = ready

    async def latest_evaluation(self) -> PublicEvaluationDashboard | None:
        if self.error:
            raise self.error
        return self.evaluation

    async def trace(self, run_id: str) -> PublicRunTrace | None:
        del run_id
        if self.error:
            raise self.error
        return self.run_trace

    async def ready(self) -> bool:
        if self.error:
            raise self.error
        return self.is_ready


def _client(reader: StubDashboardReader, **settings_overrides: Any) -> ASGITestClient:
    settings = Settings(
        connectors_dir=REPOSITORY_ROOT / "connectors",
        persist_runs=False,
        **settings_overrides,
    )
    return ASGITestClient(create_app(settings=settings, dashboard_reader=reader))


def test_dashboard_routes_return_stable_not_found_errors() -> None:
    client = _client(StubDashboardReader())

    evaluation = client.get("/api/v1/evaluations/latest")
    trace = client.get(f"/api/v1/runs/{RUN_ID}/trace")

    assert evaluation.status_code == 404
    assert evaluation.json()["detail"]["code"] == "EVALUATION_NOT_FOUND"
    assert trace.status_code == 404
    assert trace.json()["detail"]["code"] == "TRACE_NOT_FOUND"


def test_dashboard_routes_redact_database_failure() -> None:
    client = _client(StubDashboardReader(error=SQLAlchemyError("password=database-secret")))

    response = client.get("/api/v1/evaluations/latest")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "DATASTORE_UNAVAILABLE"
    assert "database-secret" not in response.text


def test_cors_allows_only_configured_frontend() -> None:
    client = _client(
        StubDashboardReader(),
        cors_allowed_origins=["https://dashboard.example"],
    )

    allowed = client.get(
        "/api/v1/health",
        headers={"Origin": "https://dashboard.example"},
    )
    denied = client.get(
        "/api/v1/health",
        headers={"Origin": "https://attacker.example"},
    )

    assert allowed.headers["access-control-allow-origin"] == "https://dashboard.example"
    assert "access-control-allow-origin" not in denied.headers


def test_cors_preflight_allows_post_and_authorization_only_for_allowlist() -> None:
    client = _client(
        StubDashboardReader(),
        cors_allowed_origins=["https://dashboard.example"],
    )

    response = client.options(
        "/api/v1/runs",
        headers={
            "Origin": "https://dashboard.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization, Content-Type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://dashboard.example"
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "Authorization" in response.headers["access-control-allow-headers"]


def test_settings_reject_wildcard_cors() -> None:
    with pytest.raises(ValidationError, match="não aceita wildcard"):
        Settings(cors_allowed_origins=["*"])


def test_ready_returns_503_when_database_migration_is_not_current() -> None:
    client = _client(StubDashboardReader(ready=False))

    response = client.get("/api/v1/ready")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "SERVICE_NOT_READY"
