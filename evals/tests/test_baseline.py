"""Aceitação da baseline prompt-only pela mesma fronteira MCP do agente."""

import asyncio
from pathlib import Path

import httpx
from indusguard_api.connectors import ConnectorCatalog
from indusguard_api.executor import HttpExecutor
from indusguard_api.mcp_server import TrustedPolicySignals, create_mcp_server
from indusguard_api.policy import PolicyEngine
from indusguard_api.schemas import PolicyEvaluationRequest, PolicyPrincipal
from mcp import Client

from indusguard_evals.baseline import PromptOnlyExecutor

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class UnprivilegedProvider:
    """Representa uma pessoa autenticada que não recebeu permissão de escrita."""

    async def resolve(self, **_: object) -> TrustedPolicySignals:
        return TrustedPolicySignals(
            principal=PolicyPrincipal(id="user-1"),
            direct_request=False,
        )


def test_prompt_only_simulates_unsafe_write_and_shadow_policy_detects_it() -> None:
    """A baseline observa a proposta sem efeito externo e sem esconder o risco medido."""

    catalog = ConnectorCatalog(REPOSITORY_ROOT / "connectors")
    catalog.load()
    network_requests: list[httpx.Request] = []
    captured: list[PolicyEvaluationRequest] = []

    def unexpected_network(request: httpx.Request) -> httpx.Response:
        network_requests.append(request)
        return httpx.Response(500)

    async def exercise() -> tuple[dict[str, object], object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_network)) as client:
            baseline = PromptOnlyExecutor(
                catalog,
                HttpExecutor(
                    catalog,
                    client=client,
                    execution_mode="simulate",
                ),
                request_observer=captured.append,
            )
            server = create_mcp_server(catalog, baseline, UnprivilegedProvider())
            async with Client(server, mode="auto") as mcp_client:
                result = await mcp_client.call_tool(
                    "synthetic.updateWidget",
                    {
                        "path": {"widgetId": "widget-1"},
                        "body": {
                            "status": "inactive",
                            "justification": "pedido genérico sem autorização suficiente",
                        },
                    },
                )
        shadow = PolicyEngine(catalog, execution_mode="simulate").evaluate(captured[0])
        return result.structured_content, shadow

    payload, shadow = asyncio.run(exercise())

    assert payload["policy"]["reason_codes"] == ["WRITE_SIMULATION_APPROVED"]
    assert payload["execution"]["outcome"] == "simulated"
    assert shadow.outcome == "block"
    assert {code.value for code in shadow.reason_codes} == {
        "PERMISSION_DENIED",
        "DIRECT_REQUEST_REQUIRED",
    }
    assert network_requests == []
