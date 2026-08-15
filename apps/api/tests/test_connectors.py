from pathlib import Path

import pytest
from conftest import ASGITestClient

from indusguard_api.connectors import ConnectorCatalog, ConnectorValidationError


def test_lists_both_declarative_connectors(client: ASGITestClient) -> None:
    response = client.get("/api/v1/connectors")

    assert response.status_code == 200
    connectors = {connector["id"]: connector for connector in response.json()}
    assert set(connectors) == {"synthetic", "tractian"}
    assert connectors["synthetic"]["operation_count"] == 2
    assert connectors["tractian"]["operation_count"] == 18


def test_tractian_keeps_get_and_patch_on_same_path(client: ASGITestClient) -> None:
    response = client.get("/api/v1/connectors/tractian/operations")

    assert response.status_code == 200
    operations = {operation["operation_id"]: operation for operation in response.json()}
    assert operations["getAsset"]["method"] == "GET"
    assert operations["updateAssetConfig"]["method"] == "PATCH"
    assert operations["updateAssetConfig"]["permission"] == "action_high"
    assert operations["updateAssetConfig"]["requires_confirmation"] is True


def test_unknown_connector_returns_404(client: ASGITestClient) -> None:
    response = client.get("/api/v1/connectors/unknown/operations")

    assert response.status_code == 404
    assert response.json()["detail"] == "conector 'unknown' não encontrado"


def test_unconfigured_operations_are_disabled(tmp_path: Path) -> None:
    connector_dir = tmp_path / "example"
    connector_dir.mkdir()
    (connector_dir / "profile.yaml").write_text(
        """
id: example
name: Example
description: Connector fixture
openapi: ./openapi.yaml
auth:
  type: none
operations: {}
""".strip(),
        encoding="utf-8",
    )
    (connector_dir / "openapi.yaml").write_text(
        """
openapi: 3.1.0
info: {title: Example, version: 1.0.0}
paths:
  /items:
    get:
      operationId: listItems
      responses:
        '200': {description: OK}
    post:
      operationId: createItem
      responses:
        '200': {description: OK}
""".strip(),
        encoding="utf-8",
    )

    catalog = ConnectorCatalog(tmp_path)
    catalog.load()
    operations = catalog.get("example").operations

    assert all(operation.enabled is False for operation in operations)


def test_duplicate_yaml_keys_are_rejected(tmp_path: Path) -> None:
    connector_dir = tmp_path / "duplicate"
    connector_dir.mkdir()
    (connector_dir / "profile.yaml").write_text(
        """
id: duplicate
id: overwritten
name: Duplicate
description: Connector fixture
openapi: ./openapi.yaml
auth: {type: none}
operations: {}
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConnectorValidationError, match="chave YAML duplicada 'id'"):
        ConnectorCatalog(tmp_path).load()
