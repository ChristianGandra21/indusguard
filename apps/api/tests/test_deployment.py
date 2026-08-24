"""Contratos estáticos que impedem o deployment de enfraquecer fronteiras do produto."""

import re

import yaml
from conftest import REPOSITORY_ROOT


def test_render_blueprint_keeps_public_runtime_free_simulated_and_secret_driven() -> None:
    blueprint = yaml.safe_load((REPOSITORY_ROOT / "render.yaml").read_text(encoding="utf-8"))
    services = {service["name"]: service for service in blueprint["services"]}

    api = services["indusguard-api"]
    web = services["indusguard-web"]
    api_env = {item["key"]: item for item in api["envVars"]}
    web_env = {item["key"]: item for item in web["envVars"]}

    assert api["runtime"] == "docker"
    assert api["plan"] == "free"
    assert api["autoDeployTrigger"] == "checksPass"
    assert api_env["INDUSGUARD_EXECUTION_MODE"]["value"] == "simulate"
    assert api_env["INDUSGUARD_PUBLIC_CONNECTOR_IDS"]["value"] == '["synthetic"]'
    for secret in (
        "INDUSGUARD_DATABASE_URL",
        "INDUSGUARD_OWNER_TOKEN",
        "GROQ_API_KEY",
        "INDUSGUARD_CORS_ALLOWED_ORIGINS",
    ):
        assert api_env[secret] == {"key": secret, "sync": False}

    assert web["runtime"] == "static"
    assert web["autoDeployTrigger"] == "checksPass"
    assert web_env["NEXT_PUBLIC_INDUSGUARD_API_URL"] == {
        "key": "NEXT_PUBLIC_INDUSGUARD_API_URL",
        "sync": False,
    }


def test_runtime_image_contract_is_non_root_migrated_and_isolated() -> None:
    dockerfile = (REPOSITORY_ROOT / "deploy/api.Dockerfile").read_text(encoding="utf-8")
    entrypoint = (REPOSITORY_ROOT / "deploy/start-api.sh").read_text(encoding="utf-8")
    ignored = (REPOSITORY_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert dockerfile.count("FROM python:3.12-slim") == 2
    assert "USER 10001:10001" in dockerfile
    assert "COPY connectors ./connectors" in dockerfile
    assert "COPY evals" not in dockerfile
    assert entrypoint.index("alembic -c") < entrypoint.index("exec uvicorn")
    for path in ("evals", "apps/web", ".env", ".data", "**/*.parquet"):
        assert path in ignored


def test_ci_actions_are_pinned_to_immutable_commit_shas() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    references = re.findall(r"^\s*uses:\s*([^\s#]+)", workflow, flags=re.MULTILINE)

    assert references
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference) for reference in references)
    assert "severity: HIGH,CRITICAL" in workflow
    assert "format: spdx-json" in workflow
