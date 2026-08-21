"""Laboratório didático: acompanha uma execução Tractian sem acessar a internet.

Execute a partir da raiz do repositório:

    PYTHONPATH=apps/api/src .venv/bin/python apps/api/examples/executor_walkthrough.py

O exemplo usa o conector Tractian real versionado no projeto, mas substitui a API externa por
``httpx.MockTransport``. Assim, é possível observar exatamente o request produzido pelo executor.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from indusguard_api.connectors import ConnectorCatalog
from indusguard_api.executor import HttpExecutor
from indusguard_api.schemas import ExecutionArguments, OperationExecutionRequest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def fake_tractian_api(request: httpx.Request) -> httpx.Response:
    """Mostra o request final e devolve uma resposta Tractian sintética."""

    print("\n2. REQUEST HTTP QUE O EXECUTOR PREPAROU")
    print(f"   método : {request.method}")
    print(f"   URL    : {request.url}")
    print(f"   usuário: {request.headers['x-user-id']}")

    return httpx.Response(
        200,
        json={
            "status": "complete",
            "data": {
                "id": "asset_M101",
                "name": "Motor principal da forja",
                "criticality": "high",
            },
        },
    )


async def main() -> None:
    """Carrega o catálogo, cria a entrada e executa a operação ``getAsset``."""

    catalog = ConnectorCatalog(REPOSITORY_ROOT / "connectors")
    catalog.load()

    execution_request = OperationExecutionRequest(
        connector_id="tractian",
        operation_id="getAsset",
        arguments=ExecutionArguments(
            path={"assetId": "asset_M101"},
            query={"seed": "case-01"},
        ),
        context={"user_id": "usr_001"},
    )

    print("1. PEDIDO INTERNO RECEBIDO PELO EXECUTOR")
    print(execution_request.model_dump_json(indent=2))
    print(
        "   campos de arguments fornecidos explicitamente: "
        f"{sorted(execution_request.arguments.model_fields_set)}"
    )

    transport = httpx.MockTransport(fake_tractian_api)
    async with httpx.AsyncClient(transport=transport) as client:
        executor = HttpExecutor(
            catalog,
            environment={"TRACTIAN_API_URL": "http://localhost:8000"},
            client=client,
        )
        result = await executor.execute(execution_request)

    print("\n3. ENVELOPE COMUM DEVOLVIDO PELO EXECUTOR")
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
