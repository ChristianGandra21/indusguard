"""Aceitação do host público sem revelar as dependências escondidas pelo módulo."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from conftest import REPOSITORY_ROOT, ASGITestClient
from pydantic import ValidationError

from indusguard_api.agent import (
    AgentDecision,
    AgentFinalAnswer,
    AgentIntentDecision,
    AgentPlannedToolCall,
    AgentPlanStep,
    AgentRunMetrics,
    AgentRunRequest,
    AgentRunResult,
    AgentRunStatus,
    AgentRuntime,
    AgentTerminationReason,
    ScriptedAgentModelGateway,
    TrustedRunContext,
)
from indusguard_api.connectors import ConnectorCatalog
from indusguard_api.executor import HttpExecutor
from indusguard_api.main import create_app
from indusguard_api.observability import OpenTelemetryRuntime
from indusguard_api.policy import GuardedExecutor, PolicyEngine
from indusguard_api.public_runs import (
    PublicRunHost,
    PublicRunQuotaDecision,
    PublicRunRequest,
    SqlAlchemyPublicRunQuota,
)
from indusguard_api.settings import Settings
from indusguard_api.synthetic_upstream import create_synthetic_upstream

OWNER_TOKEN = "owner-token-with-at-least-thirty-two-characters"


class NeverCalledRuntime:
    """Falha o teste caso autenticação inválida alcance o agente."""

    async def run(self, *_: Any, **__: Any) -> Any:
        raise AssertionError("runtime não deveria ser chamado")


class NeverCalledQuota:
    """Falha o teste caso uma tentativa não autenticada consuma quota."""

    async def consume(self, *_: Any, **__: Any) -> PublicRunQuotaDecision:
        raise AssertionError("quota não deveria ser consultada")


class AlwaysAllowQuota:
    async def consume(self, *_: Any, **__: Any) -> PublicRunQuotaDecision:
        return PublicRunQuotaDecision(
            allowed=True,
            accepted_runs=1,
            reset_at=datetime.now(UTC) + timedelta(hours=1),
            retry_after_seconds=0,
        )

    async def ready(self) -> bool:
        return True


class CountingQuota(AlwaysAllowQuota):
    def __init__(self) -> None:
        self.calls = 0

    async def consume(self, *_: Any, **__: Any) -> PublicRunQuotaDecision:
        self.calls += 1
        return await super().consume()


class DenyQuota(AlwaysAllowQuota):
    async def consume(self, *_: Any, **__: Any) -> PublicRunQuotaDecision:
        return PublicRunQuotaDecision(
            allowed=False,
            accepted_runs=3,
            reset_at=datetime.now(UTC) + timedelta(minutes=5),
            retry_after_seconds=300,
        )


class CapturingRuntime:
    def __init__(self) -> None:
        self.requests: list[AgentRunRequest] = []
        self.trusted_contexts: list[TrustedRunContext] = []

    async def run(
        self,
        request: AgentRunRequest,
        trusted_context: TrustedRunContext,
    ) -> AgentRunResult:
        self.requests.append(request)
        self.trusted_contexts.append(trusted_context)
        return _minimal_result(
            connector_id=request.connector_id,
            intent_id="agir",
            decision=AgentDecision.ACT,
        )


def _minimal_result(
    *,
    connector_id: str = "synthetic",
    intent_id: str = "consultar",
    decision: AgentDecision = AgentDecision.ORIENT,
) -> AgentRunResult:
    now = datetime.now(UTC)
    return AgentRunResult(
        run_id=str(uuid4()),
        started_at=now,
        completed_at=now,
        connector_id=connector_id,
        status=AgentRunStatus.COMPLETED,
        intent=AgentIntentDecision(intent_id=intent_id),
        decision=decision,
        answer="Execução concluída.",
        evidence_ids=[],
        evidence=[],
        uncertainties=[],
        tool_calls=[],
        metrics=AgentRunMetrics(
            model="fake-public-model",
            prompt_version="agent-v1",
            domain_version="domain-v1",
            policy_version="policy-v1",
            model_calls=2,
            tool_calls=0,
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_ms=1,
            termination_reason=AgentTerminationReason.COMPLETED,
            truncations=0,
        ),
    )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        connectors_dir=REPOSITORY_ROOT / "connectors",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'public-runs.db'}",
        persist_runs=False,
        trace_jsonl_enabled=False,
        public_runs_enabled=True,
        owner_token=OWNER_TOKEN,
    )


def test_missing_bearer_is_rejected_before_runtime_and_quota(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    host = PublicRunHost(
        catalog=None,
        runtime=NeverCalledRuntime(),
        quota=NeverCalledQuota(),
        enabled=True,
        owner_token=OWNER_TOKEN,
        public_connector_ids=["synthetic"],
    )
    client = ASGITestClient(create_app(settings=settings, public_run_host=host))

    response = client.post(
        "/api/v1/runs",
        json=PublicRunRequest(
            connector_id="synthetic",
            message="Qual é o estado do widget?",
        ).model_dump(mode="json"),
    )

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_REQUIRED"
    assert response.headers["cache-control"] == "no-store"


def test_invalid_bearer_never_echoes_the_secret(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    host = PublicRunHost(
        catalog=None,
        runtime=NeverCalledRuntime(),
        quota=NeverCalledQuota(),
        enabled=True,
        owner_token=OWNER_TOKEN,
        public_connector_ids=["synthetic"],
    )
    client = ASGITestClient(create_app(settings=settings, public_run_host=host))

    response = client.post(
        "/api/v1/runs",
        headers={"Authorization": "Bearer token-super-secreto-invalido"},
        json={"connector_id": "synthetic", "message": "Consulte o widget."},
    )

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_INVALID"
    assert "token-super-secreto-invalido" not in response.text


def test_token_and_trusted_claims_are_absent_from_openapi_and_spans(tmp_path: Path) -> None:
    trace_path = tmp_path / "http-spans.jsonl"
    telemetry = OpenTelemetryRuntime(
        service_name="indusguard-public-test",
        jsonl_path=trace_path,
    )
    settings = _settings(tmp_path)
    host = PublicRunHost(
        catalog=None,
        runtime=NeverCalledRuntime(),
        quota=NeverCalledQuota(),
        enabled=True,
        owner_token=OWNER_TOKEN,
        public_connector_ids=["synthetic"],
        telemetry=telemetry,
    )
    application = create_app(
        settings=settings,
        public_run_host=host,
        telemetry=telemetry,
    )
    client = ASGITestClient(application)

    response = client.post(
        "/api/v1/runs",
        headers={"Authorization": "Bearer span-token-super-secreto"},
        json={"connector_id": "synthetic", "message": "Consulte o widget."},
    )
    telemetry.force_flush()
    telemetry.shutdown()

    openapi = application.openapi()
    serialized_schema = json.dumps(openapi)
    request_properties = set(openapi["components"]["schemas"]["PublicRunRequest"]["properties"])
    serialized_spans = trace_path.read_text(encoding="utf-8")
    assert response.status_code == 401
    assert request_properties == {
        "connector_id",
        "message",
        "seed",
        "context",
        "direct_request",
    }
    assert OWNER_TOKEN not in serialized_schema
    assert "span-token-super-secreto" not in serialized_schema
    assert "owner_token" not in serialized_schema
    for forbidden in (
        OWNER_TOKEN,
        "span-token-super-secreto",
        "principal",
        "permissions",
        "resource_scopes",
        "confirmation",
        "action_digest",
    ):
        assert forbidden not in serialized_spans


def test_enabled_host_without_groq_key_returns_model_not_configured(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    settings = _settings(tmp_path)
    application = create_app(settings=settings)
    client = ASGITestClient(application)

    config = client.get("/api/v1/playground/config")
    response = client.post(
        "/api/v1/runs",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
        json={"connector_id": "synthetic", "message": "Consulte o widget."},
    )

    assert config.status_code == 200
    assert config.json()["enabled"] is True
    assert config.json()["model_configured"] is False
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "MODEL_NOT_CONFIGURED"


def test_rejects_unknown_context_and_client_controlled_claims(tmp_path: Path) -> None:
    catalog = ConnectorCatalog(REPOSITORY_ROOT / "connectors")
    catalog.load()
    host = PublicRunHost(
        catalog=catalog,
        runtime=NeverCalledRuntime(),
        quota=NeverCalledQuota(),
        enabled=True,
        owner_token=OWNER_TOKEN,
        public_connector_ids=["synthetic"],
    )
    client = ASGITestClient(create_app(settings=_settings(tmp_path), public_run_host=host))
    headers = {"Authorization": f"Bearer {OWNER_TOKEN}"}

    unknown_context = client.post(
        "/api/v1/runs",
        headers=headers,
        json={
            "connector_id": "synthetic",
            "message": "Consulte o widget.",
            "context": {"admin": True},
        },
    )
    injected_claim = client.post(
        "/api/v1/runs",
        headers=headers,
        json={
            "connector_id": "synthetic",
            "message": "Consulte o widget.",
            "permissions": ["action_high"],
            "confirmation": {"digest": "client-controlled"},
        },
    )

    assert unknown_context.status_code == 422
    assert unknown_context.json()["detail"]["code"] == "CONTEXT_INVALID"
    assert injected_claim.status_code == 422
    assert injected_claim.json()["detail"]["code"] == "CONTEXT_INVALID"
    assert "client-controlled" not in injected_claim.text


def test_rate_limit_returns_stable_code_and_retry_after(tmp_path: Path) -> None:
    catalog = ConnectorCatalog(REPOSITORY_ROOT / "connectors")
    catalog.load()
    host = PublicRunHost(
        catalog=catalog,
        runtime=NeverCalledRuntime(),
        quota=DenyQuota(),
        enabled=True,
        owner_token=OWNER_TOKEN,
        public_connector_ids=["synthetic"],
    )
    client = ASGITestClient(create_app(settings=_settings(tmp_path), public_run_host=host))

    response = client.post(
        "/api/v1/runs",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
        json={"connector_id": "synthetic", "message": "Consulte o widget."},
    )

    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "RUN_RATE_LIMITED"
    assert response.headers["retry-after"] == "300"


def test_public_settings_require_strong_token_and_known_public_connector() -> None:
    with pytest.raises(ValidationError, match="ao menos 32 caracteres"):
        Settings(_env_file=None, public_runs_enabled=True, owner_token="short")
    settings = Settings(_env_file=None, public_connector_ids=["synthetic", "tractian"])
    assert settings.public_connector_ids == ["synthetic", "tractian"]
    with pytest.raises(ValidationError, match="conectores não suportados"):
        Settings(_env_file=None, public_connector_ids=["unknown"])
    with pytest.raises(ValidationError, match="EXECUTION_MODE=simulate"):
        Settings(
            _env_file=None,
            public_runs_enabled=True,
            owner_token=OWNER_TOKEN,
            execution_mode="execute",
        )


def test_public_tractian_connector_uses_server_context_and_hidden_user_claim(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response, CapturingRuntime]:
        catalog = ConnectorCatalog(REPOSITORY_ROOT / "connectors")
        catalog.load()
        runtime = CapturingRuntime()
        host = PublicRunHost(
            catalog=catalog,
            runtime=runtime,
            quota=AlwaysAllowQuota(),
            enabled=True,
            owner_token=OWNER_TOKEN,
            public_connector_ids=["synthetic", "tractian"],
        )
        application = create_app(settings=_settings(tmp_path), public_run_host=host)
        async with (
            application.router.lifespan_context(application),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application),
                base_url="http://testserver",
            ) as client,
        ):
            config = await client.get("/api/v1/playground/config")
            response = await client.post(
                "/api/v1/runs",
                headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
                json={
                    "connector_id": "tractian",
                    "message": "Quero análise especializada desse compressor.",
                    "context": {
                        "user_id": "browser-controlled",
                        "company_id": "comp_petro_delta",
                        "asset_id": "asset_C710",
                        "case_id": "case_tkt_exe_13",
                    },
                    "direct_request": True,
                },
            )
        return config, response, runtime

    config, response, runtime = asyncio.run(scenario())

    assert config.status_code == 200
    connectors = {item["id"]: item for item in config.json()["connectors"]}
    assert connectors["tractian"]["context_fields"] == ["company_id", "asset_id", "case_id"]
    assert response.status_code == 200
    assert response.json()["connector_id"] == "tractian"
    assert runtime.requests[0].connector_id == "tractian"
    trusted = runtime.trusted_contexts[0]
    assert trusted.execution_context == {
        "user_id": "portfolio-owner",
        "company_id": "comp_petro_delta",
        "asset_id": "asset_C710",
        "case_id": "case_tkt_exe_13",
    }
    assert trusted.resource_scopes == {
        "company_id": "comp_petro_delta",
        "asset_id": "asset_C710",
        "case_id": "case_tkt_exe_13",
    }
    assert trusted.principal is not None
    assert trusted.principal.permissions == ["read", "action_low", "action_high", "escalate"]
    assert trusted.principal.scopes == {
        "company_id": "comp_petro_delta",
        "asset_id": "asset_C710",
        "case_id": "case_tkt_exe_13",
    }
    assert trusted.direct_request is True
    assert "browser-controlled" not in response.text


def test_persistent_quota_allows_three_runs_and_resets_after_one_hour(tmp_path: Path) -> None:
    clock = [datetime(2026, 8, 24, 12, 0, tzinfo=UTC)]

    async def scenario() -> tuple[list[PublicRunQuotaDecision], PublicRunQuotaDecision]:
        quota = SqlAlchemyPublicRunQuota.from_url(
            f"sqlite+aiosqlite:///{tmp_path / 'quota.db'}",
            now=lambda: clock[0],
        )
        await quota.create_schema_for_tests()
        decisions = [await quota.consume(subject="owner", limit=3) for _ in range(4)]
        clock[0] += timedelta(hours=1)
        reset = await quota.consume(subject="owner", limit=3)
        await quota.close()
        return decisions, reset

    decisions, reset = asyncio.run(scenario())

    assert [decision.allowed for decision in decisions] == [True, True, True, False]
    assert decisions[-1].accepted_runs == 3
    assert decisions[-1].retry_after_seconds == 3600
    assert reset.allowed is True
    assert reset.accepted_runs == 1


def test_authenticated_get_crosses_langgraph_mcp_policy_and_synthetic_asgi(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[httpx.Response, ScriptedAgentModelGateway, Any]:
        catalog = ConnectorCatalog(REPOSITORY_ROOT / "connectors")
        catalog.load()
        upstream = create_synthetic_upstream()
        gateway = ScriptedAgentModelGateway(
            classification=AgentIntentDecision(intent_id="consultar"),
            plans=[
                AgentPlanStep(
                    tool_calls=[
                        AgentPlannedToolCall(
                            alias="synthetic__getWidget",
                            arguments={"path": {"widgetId": "widget-1"}},
                        )
                    ]
                ),
                AgentPlanStep(done=True),
            ],
            final_answer=AgentFinalAnswer(
                answer="O widget está ativo [ev-001].",
                decision=AgentDecision.ORIENT,
                evidence_ids=["ev-001"],
            ),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=upstream),
            base_url="http://localhost:9000",
        ) as upstream_client:
            guarded = GuardedExecutor(
                PolicyEngine(catalog, execution_mode="simulate"),
                HttpExecutor(
                    catalog,
                    client=upstream_client,
                    environment={"SYNTHETIC_API_URL": "http://localhost:9000"},
                    execution_mode="simulate",
                ),
            )
            runtime = AgentRuntime(catalog, guarded, gateway)
            host = PublicRunHost(
                catalog=catalog,
                runtime=runtime,
                quota=AlwaysAllowQuota(),
                enabled=True,
                owner_token=OWNER_TOKEN,
                public_connector_ids=["synthetic"],
            )
            application = create_app(
                settings=_settings(tmp_path),
                public_run_host=host,
            )
            async with (
                application.router.lifespan_context(application),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=application),
                    base_url="http://testserver",
                ) as client,
            ):
                response = await client.post(
                    "/api/v1/runs",
                    headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
                    json={
                        "connector_id": "synthetic",
                        "message": "Qual é o estado do widget widget-1?",
                        "context": {
                            "user_id": "attacker-controlled",
                            "widget_id": "widget-1",
                        },
                    },
                )
        return response, gateway, upstream

    response, gateway, upstream = asyncio.run(scenario())

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "O widget está ativo [ev-001]."
    assert body["evidence"][0]["result"]["execution"]["data"] == {
        "id": "widget-1",
        "status": "active",
    }
    assert body["policy_decisions"][0]["outcome"] == "allow"
    assert "action_digest" not in response.text
    assert upstream.state.read_count == 1
    assert upstream.state.write_count == 0
    planning_context = gateway.seen_planning_contexts[0]
    assert planning_context.context["user_id"] == "portfolio-owner"
    assert planning_context.context["widget_id"] == "widget-1"


def test_authenticated_write_is_simulated_without_reaching_synthetic_asgi(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[httpx.Response, Any]:
        catalog = ConnectorCatalog(REPOSITORY_ROOT / "connectors")
        catalog.load()
        upstream = create_synthetic_upstream()
        gateway = ScriptedAgentModelGateway(
            classification=AgentIntentDecision(intent_id="atualizar"),
            plans=[
                AgentPlanStep(
                    tool_calls=[
                        AgentPlannedToolCall(
                            alias="synthetic__updateWidget",
                            arguments={
                                "path": {"widgetId": "widget-1"},
                                "body": {
                                    "status": "inactive",
                                    "justification": (
                                        "Manutenção preventiva solicitada pelo proprietário."
                                    ),
                                },
                            },
                        )
                    ]
                ),
                AgentPlanStep(done=True),
            ],
            final_answer=AgentFinalAnswer(
                answer="A alteração foi apenas simulada [ev-001].",
                decision=AgentDecision.ACT,
                evidence_ids=["ev-001"],
            ),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=upstream),
            base_url="http://localhost:9000",
        ) as upstream_client:
            runtime = AgentRuntime(
                catalog,
                GuardedExecutor(
                    PolicyEngine(catalog, execution_mode="simulate"),
                    HttpExecutor(
                        catalog,
                        client=upstream_client,
                        environment={"SYNTHETIC_API_URL": "http://localhost:9000"},
                        execution_mode="simulate",
                    ),
                ),
                gateway,
            )
            host = PublicRunHost(
                catalog=catalog,
                runtime=runtime,
                quota=AlwaysAllowQuota(),
                enabled=True,
                owner_token=OWNER_TOKEN,
                public_connector_ids=["synthetic"],
            )
            application = create_app(settings=_settings(tmp_path), public_run_host=host)
            async with (
                application.router.lifespan_context(application),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=application),
                    base_url="http://testserver",
                ) as client,
            ):
                response = await client.post(
                    "/api/v1/runs",
                    headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
                    json={
                        "connector_id": "synthetic",
                        "message": "Desative o widget widget-1 para manutenção preventiva.",
                        "context": {"widget_id": "widget-1"},
                        "direct_request": True,
                    },
                )
        return response, upstream

    response, upstream = asyncio.run(scenario())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["policy_decisions"][0]["outcome"] == "simulate"
    assert body["evidence"][0]["result"]["execution"]["outcome"] == "simulated"
    assert body["evidence"][0]["result"]["execution"]["attempts"] == 0
    assert upstream.state.read_count == 0
    assert upstream.state.write_count == 0
    assert "action_digest" not in response.text


def test_third_concurrent_run_is_rejected_without_consuming_quota(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[httpx.Response], httpx.Response, int]:
        catalog = ConnectorCatalog(REPOSITORY_ROOT / "connectors")
        catalog.load()
        two_started = asyncio.Event()
        release = asyncio.Event()

        class BlockingRuntime:
            def __init__(self) -> None:
                self.started = 0

            async def run(self, *_: Any, **__: Any) -> AgentRunResult:
                self.started += 1
                if self.started == 2:
                    two_started.set()
                await release.wait()
                return _minimal_result()

        quota = CountingQuota()
        host = PublicRunHost(
            catalog=catalog,
            runtime=BlockingRuntime(),
            quota=quota,
            enabled=True,
            owner_token=OWNER_TOKEN,
            public_connector_ids=["synthetic"],
            concurrency_limit=2,
        )
        application = create_app(settings=_settings(tmp_path), public_run_host=host)
        payload = {"connector_id": "synthetic", "message": "Consulte o widget."}
        headers = {"Authorization": f"Bearer {OWNER_TOKEN}"}
        async with (
            application.router.lifespan_context(application),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application),
                base_url="http://testserver",
            ) as client,
        ):
            accepted_tasks = [
                asyncio.create_task(client.post("/api/v1/runs", json=payload, headers=headers))
                for _ in range(2)
            ]
            await asyncio.wait_for(two_started.wait(), timeout=2)
            rejected = await client.post("/api/v1/runs", json=payload, headers=headers)
            release.set()
            accepted = await asyncio.gather(*accepted_tasks)
        return accepted, rejected, quota.calls

    accepted, rejected, quota_calls = asyncio.run(scenario())

    assert [response.status_code for response in accepted] == [200, 200]
    assert rejected.status_code == 429
    assert rejected.json()["detail"]["code"] == "RUN_CONCURRENCY_LIMIT"
    assert quota_calls == 2
