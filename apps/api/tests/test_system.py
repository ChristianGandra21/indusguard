"""Testes dos sinais operacionais usados por desenvolvimento e deployment."""

from conftest import ASGITestClient


def test_health(client: ASGITestClient) -> None:
    """Liveness deve manter um contrato mínimo e estável."""

    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_ready_reports_loaded_connectors(client: ASGITestClient) -> None:
    """Readiness comprova que o catálogo foi carregado durante o lifespan."""

    response = client.get("/api/v1/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "connector_count": 2,
        "database_ready": True,
        "public_run_host_ready": True,
    }


def test_version_defaults_to_safe_simulation(client: ASGITestClient) -> None:
    """Uma instalação nova nunca deve começar habilitada para mutações reais."""

    response = client.get("/api/v1/version")

    assert response.status_code == 200
    assert response.json() == {
        "version": "0.1.0",
        "environment": "development",
        "execution_mode": "simulate",
    }
