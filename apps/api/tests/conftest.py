"""Fixtures compartilhadas pelos testes HTTP do backend."""

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from indusguard_api.main import create_app

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


class ASGITestClient:
    """Cliente síncrono de teste que chama a aplicação diretamente em memória.

    ``ASGITransport`` evita abrir portas reais e deixa os testes rápidos. Entrar manualmente no
    lifespan é importante porque é nele que os conectores são carregados, exatamente como no
    startup de produção.
    """

    def __init__(self, app: FastAPI) -> None:
        self.app = app

    def get(self, path: str, *, headers: dict[str, str] | None = None) -> httpx.Response:
        """Executa um GET ASGI e devolve a resposta HTTP normal do httpx."""

        async def request() -> httpx.Response:
            async with self.app.router.lifespan_context(self.app):
                transport = httpx.ASGITransport(app=self.app)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                ) as client:
                    return await client.get(path, headers=headers)

        return asyncio.run(request())

    def post(
        self,
        path: str,
        *,
        json: Any,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Executa um POST mantendo o mesmo seam ASGI dos testes de leitura."""

        async def request() -> httpx.Response:
            async with self.app.router.lifespan_context(self.app):
                transport = httpx.ASGITransport(app=self.app)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                ) as client:
                    return await client.post(path, json=json, headers=headers)

        return asyncio.run(request())

    def options(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Executa o preflight CORS usado antes do POST autenticado."""

        async def request() -> httpx.Response:
            async with self.app.router.lifespan_context(self.app):
                transport = httpx.ASGITransport(app=self.app)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                ) as client:
                    return await client.options(path, headers=headers)

        return asyncio.run(request())


@pytest.fixture
def client() -> ASGITestClient:
    """Cria uma aplicação nova por teste para impedir vazamento de estado entre casos."""

    return ASGITestClient(create_app(connectors_dir=REPOSITORY_ROOT / "connectors"))
