"""Testes do contrato declarativo e dos defaults seguros dos conectores."""

from pathlib import Path

import pytest
from conftest import REPOSITORY_ROOT, ASGITestClient

from indusguard_api.connectors import ConnectorCatalog, ConnectorValidationError


def test_lists_both_declarative_connectors(client: ASGITestClient) -> None:
    """Prova que uma API nova é descoberta sem registro específico em Python."""

    response = client.get("/api/v1/connectors")

    assert response.status_code == 200
    connectors = {connector["id"]: connector for connector in response.json()}
    assert set(connectors) == {"synthetic", "tractian"}
    assert connectors["synthetic"]["operation_count"] == 2
    assert connectors["tractian"]["operation_count"] == 18


def test_tractian_keeps_get_and_patch_on_same_path(client: ASGITestClient) -> None:
    """Protege a normalização feita no OpenAPI originalmente entregue com path duplicado."""

    response = client.get("/api/v1/connectors/tractian/operations")

    assert response.status_code == 200
    operations = {operation["operation_id"]: operation for operation in response.json()}
    assert operations["getAsset"]["method"] == "GET"
    assert operations["updateAssetConfig"]["method"] == "PATCH"
    assert operations["updateAssetConfig"]["permission"] == "action_high"
    assert operations["updateAssetConfig"]["requires_confirmation"] is True
    assert operations["updateAssetConfig"]["required_scopes"] == ["company_id"]
    assert operations["updateAssetConfig"]["justification_pointer"] == "/justification"
    scoped_writes = {
        "updateAssetConfig",
        "reprocessAnalysis",
        "requestSpecialistAnalysis",
        "escalateCase",
    }
    assert all(
        operations[operation]["required_scopes"] == ["company_id"] for operation in scoped_writes
    )
    assert operations["requestRetraining"]["required_scopes"] == []


def test_unknown_connector_returns_404(client: ASGITestClient) -> None:
    """Mantém um erro HTTP explícito em vez de devolver uma lista vazia ambígua."""

    response = client.get("/api/v1/connectors/unknown/operations")

    assert response.status_code == 404
    assert response.json()["detail"] == "conector 'unknown' não encontrado"


def test_unconfigured_operations_are_disabled(tmp_path: Path) -> None:
    """Garante que endpoints recém-descobertos não sejam liberados automaticamente."""

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
    """Evita perda silenciosa de operações quando uma chave YAML é repetida."""

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


def test_context_auth_requires_field_declared_by_domain(tmp_path: Path) -> None:
    """Evita um conector cuja autenticação nunca poderia ser preenchida pela UI/contexto."""

    connector_dir = tmp_path / "contextual"
    connector_dir.mkdir()
    (connector_dir / "profile.yaml").write_text(
        """
id: contextual
name: Contextual
description: Connector fixture
openapi: ./openapi.yaml
auth:
  type: context_header
  name: x-user-id
  context_field: user_id
operations:
  getThing: {enabled: true, access: read}
""".strip(),
        encoding="utf-8",
    )
    (connector_dir / "domain.yaml").write_text(
        "context_fields: [company_id]",
        encoding="utf-8",
    )
    (connector_dir / "openapi.yaml").write_text(
        """
openapi: 3.1.0
info: {title: Contextual, version: 1.0.0}
paths:
  /thing:
    get:
      operationId: getThing
      responses:
        '200': {description: OK}
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConnectorValidationError, match="autenticação exige.*user_id"):
        ConnectorCatalog(tmp_path).load()


def test_loads_typed_domain_for_agent_runtime() -> None:
    """O agente recebe um domínio validado, não um dicionário YAML sem contrato."""

    catalog = ConnectorCatalog(REPOSITORY_ROOT / "connectors")
    catalog.load()

    domain = catalog.get_domain("tractian")

    assert domain is not None
    assert domain.id == "tractian"
    assert domain.language == "pt-BR"
    assert domain.terminology["asset"].startswith("ativo industrial")
    assert [intent.id for intent in domain.intents] == [
        "contextualizar",
        "investigar",
        "agir",
        "escalar",
    ]
    assert domain.intents[1].evidence_operations == [
        "getAsset",
        "getBaseline",
        "getDataQuality",
        "getRmsSeries",
    ]
    assert domain.intents[2].action_operations == [
        "updateAssetConfig",
        "reprocessAnalysis",
        "requestSpecialistAnalysis",
        "requestRetraining",
    ]
    assert domain.intents[2].evidence_operations == [
        "listAnalyses",
        "getAnalysis",
        "getBaseline",
    ]
    assert domain.evidence_states == [
        "complete",
        "partial",
        "inconclusive",
        "conflict",
        "unavailable",
    ]


def test_rejects_domain_reference_to_unknown_operation(tmp_path: Path) -> None:
    """Uma intenção não pode ensinar ao agente uma operação ausente do OpenAPI."""

    connector_dir = tmp_path / "example"
    connector_dir.mkdir()
    (connector_dir / "profile.yaml").write_text(
        """
id: example
name: Example
description: Connector fixture
openapi: ./openapi.yaml
auth: {type: none}
operations:
  getThing: {enabled: true, access: read}
""".strip(),
        encoding="utf-8",
    )
    (connector_dir / "openapi.yaml").write_text(
        """
openapi: 3.1.0
info: {title: Example, version: 1.0.0}
paths:
  /thing:
    get:
      operationId: getThing
      responses:
        '200': {description: OK}
""".strip(),
        encoding="utf-8",
    )
    (connector_dir / "domain.yaml").write_text(
        """
id: example
language: pt-BR
intents:
  - id: consultar
    description: Consultar um recurso.
    evidence_operations: [missingOperation]
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConnectorValidationError, match="operationId inexistente"):
        ConnectorCatalog(tmp_path).load()


@pytest.mark.parametrize(
    ("domain", "message"),
    [
        (
            """
id: another
language: pt-BR
intents: []
""",
            "id 'another' deve corresponder ao conector 'example'",
        ),
        (
            """
id: example
language: pt-BR
intents:
  - {id: consultar, description: Primeira intenção}
  - {id: consultar, description: Intenção duplicada}
""",
            "intents não pode conter ids duplicados",
        ),
    ],
)
def test_rejects_ambiguous_domain_identity(
    tmp_path: Path,
    domain: str,
    message: str,
) -> None:
    """Domínio com identidade divergente ou intenção duplicada falha no startup."""

    connector_dir = tmp_path / "example"
    connector_dir.mkdir()
    (connector_dir / "profile.yaml").write_text(
        """
id: example
name: Example
description: Connector fixture
openapi: ./openapi.yaml
auth: {type: none}
operations:
  getThing: {enabled: true, access: read}
""".strip(),
        encoding="utf-8",
    )
    (connector_dir / "openapi.yaml").write_text(
        """
openapi: 3.1.0
info: {title: Example, version: 1.0.0}
paths:
  /thing:
    get:
      operationId: getThing
      responses:
        '200': {description: OK}
""".strip(),
        encoding="utf-8",
    )
    (connector_dir / "domain.yaml").write_text(domain.strip(), encoding="utf-8")

    with pytest.raises(ConnectorValidationError, match=message):
        ConnectorCatalog(tmp_path).load()
