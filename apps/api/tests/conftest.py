import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from indusguard_api.main import create_app

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


class ASGITestClient:
    """Cliente síncrono de teste sem depender de uma thread auxiliar."""

    def __init__(self, app: FastAPI) -> None:
        self.app = app

    def get(self, path: str) -> httpx.Response:
        async def request() -> httpx.Response:
            async with self.app.router.lifespan_context(self.app):
                transport = httpx.ASGITransport(app=self.app)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                ) as client:
                    return await client.get(path)

        return asyncio.run(request())


@pytest.fixture
def client() -> ASGITestClient:
    return ASGITestClient(create_app(connectors_dir=REPOSITORY_ROOT / "connectors"))
