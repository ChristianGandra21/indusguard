from conftest import ASGITestClient


def test_health(client: ASGITestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_ready_reports_loaded_connectors(client: ASGITestClient) -> None:
    response = client.get("/api/v1/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "connector_count": 2}


def test_version_defaults_to_safe_simulation(client: ASGITestClient) -> None:
    response = client.get("/api/v1/version")

    assert response.status_code == 200
    assert response.json() == {
        "version": "0.1.0",
        "environment": "development",
        "execution_mode": "simulate",
    }
