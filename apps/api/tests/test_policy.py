"""Testes da fronteira determinística entre uma proposta do agente e o executor HTTP."""

import asyncio
from collections.abc import Mapping
from pathlib import Path
from textwrap import dedent
from typing import Any

import httpx
import pytest
from conftest import REPOSITORY_ROOT

from indusguard_api.connectors import ConnectorCatalog
from indusguard_api.executor import HttpExecutor
from indusguard_api.policy import GuardedExecutor, PolicyEngine
from indusguard_api.schemas import (
    ExecutionArguments,
    ExecutionOutcome,
    OperationExecutionRequest,
    PolicyConfirmation,
    PolicyEvaluationRequest,
    PolicyOutcome,
    PolicyPrincipal,
    PolicyReasonCode,
)

_NO_BODY = object()


@pytest.fixture
def catalog() -> ConnectorCatalog:
    """Usa os dois conectores reais para provar que a engine não conhece um domínio específico."""

    loaded = ConnectorCatalog(REPOSITORY_ROOT / "connectors")
    loaded.load()
    return loaded


def _execution(
    *,
    connector_id: str = "synthetic",
    operation_id: str = "updateWidget",
    path: Mapping[str, Any] | None = None,
    body: Any = None,
    context: Mapping[str, Any] | None = None,
) -> OperationExecutionRequest:
    """Cria a proposta técnica que futuramente virá do runtime do agente."""

    argument_values: dict[str, Any] = {
        "path": dict(path or {"widgetId": "widget-1"}),
    }
    if body is not _NO_BODY:
        argument_values["body"] = (
            {
                "status": "inactive",
                "justification": "manutenção preventiva solicitada pela equipe",
            }
            if body is None
            else body
        )
    return OperationExecutionRequest(
        connector_id=connector_id,
        operation_id=operation_id,
        arguments=ExecutionArguments.model_validate(argument_values),
        context=dict(context or {}),
    )


def _synthetic_write_request(
    *,
    principal: PolicyPrincipal | None = None,
    body: Any = None,
    context: Mapping[str, Any] | None = None,
    direct_request: bool = True,
    confirmation: PolicyConfirmation | None = None,
) -> PolicyEvaluationRequest:
    """Monta uma escrita válida e permite trocar somente o sinal relevante em cada teste."""

    return PolicyEvaluationRequest(
        execution=_execution(body=body, context=context),
        principal=principal or PolicyPrincipal(id="user-1", permissions=["action_high"]),
        direct_request=direct_request,
        confirmation=confirmation,
    )


def _tractian_write_request(
    *,
    principal_scopes: Mapping[str, Any] | None = None,
    resource_scopes: Mapping[str, Any] | None = None,
    context_company: Any = "company-1",
) -> PolicyEvaluationRequest:
    """Representa os três sinais independentes usados para isolamento empresarial."""

    context = {"user_id": "user-1"}
    if context_company is not None:
        context["company_id"] = context_company
    return PolicyEvaluationRequest(
        execution=_execution(
            connector_id="tractian",
            operation_id="updateAssetConfig",
            path={"assetId": "asset-1"},
            body={
                "justification": "mudança aprovada para manutenção preventiva",
                "changes": {"criticality": "high"},
            },
            context=context,
        ),
        principal=PolicyPrincipal(
            id="user-1",
            permissions=["action_high"],
            scopes=dict(
                principal_scopes if principal_scopes is not None else {"company_id": "company-1"}
            ),
        ),
        resource_scopes=dict(
            resource_scopes if resource_scopes is not None else {"company_id": "company-1"}
        ),
        direct_request=True,
    )


def _temporary_policy_catalog(tmp_path: Path, *, enabled: bool = True) -> ConnectorCatalog:
    """Conector mínimo com justificativa aninhada para provar configuração via YAML."""

    connector_dir = tmp_path / "nested"
    connector_dir.mkdir()
    (connector_dir / "profile.yaml").write_text(
        dedent(
            f"""
            id: nested
            name: Nested
            description: Fixture da policy engine
            openapi: ./openapi.yaml
            auth: {{type: none}}
            operations:
              changeItem:
                enabled: {str(enabled).lower()}
                access: write
                risk: medium
                justification_min_length: 10
                justification_pointer: /metadata/reasons/0/text
            """
        ).strip(),
        encoding="utf-8",
    )
    (connector_dir / "openapi.yaml").write_text(
        dedent(
            """
            openapi: 3.1.0
            info: {title: Nested, version: 1.0.0}
            paths:
              /items/{itemId}:
                patch:
                  operationId: changeItem
                  parameters:
                    - name: itemId
                      in: path
                      required: true
                      schema: {type: string}
                  requestBody:
                    required: true
                    content:
                      application/json:
                        schema: {type: object, additionalProperties: true}
                  responses:
                    '200': {description: OK}
            """
        ).strip(),
        encoding="utf-8",
    )
    loaded = ConnectorCatalog(tmp_path)
    loaded.load()
    return loaded


def test_allows_read_when_trusted_identity_matches_context(catalog: ConnectorCatalog) -> None:
    """A pessoa autenticada e o header derivado do contexto precisam representar a mesma pessoa."""

    request = PolicyEvaluationRequest(
        execution=_execution(
            connector_id="tractian",
            operation_id="getAsset",
            path={"assetId": "asset-1"},
            body=_NO_BODY,
            context={"user_id": "user-1"},
        ),
        principal=PolicyPrincipal(id="user-1"),
    )

    decision = PolicyEngine(catalog).evaluate(request)

    assert decision.outcome is PolicyOutcome.ALLOW
    assert decision.reason_codes == [PolicyReasonCode.READ_APPROVED]
    assert decision.action_digest is None


@pytest.mark.parametrize("principal", [None, PolicyPrincipal(id="another-user")])
def test_blocks_missing_or_inconsistent_context_identity(
    catalog: ConnectorCatalog,
    principal: PolicyPrincipal | None,
) -> None:
    """O LLM não pode escolher uma identidade diferente da autenticada pelo runtime."""

    request = PolicyEvaluationRequest(
        execution=_execution(
            connector_id="tractian",
            operation_id="getAsset",
            path={"assetId": "asset-1"},
            body=_NO_BODY,
            context={"user_id": "user-1"},
        ),
        principal=principal,
    )

    decision = PolicyEngine(catalog).evaluate(request)

    expected = (
        PolicyReasonCode.PRINCIPAL_REQUIRED
        if principal is None
        else PolicyReasonCode.PRINCIPAL_CONTEXT_MISMATCH
    )
    assert decision.outcome is PolicyOutcome.BLOCK
    assert expected in decision.reason_codes


@pytest.mark.parametrize(
    ("connector_id", "operation_id", "expected_code"),
    [
        ("missing", "updateWidget", PolicyReasonCode.CONNECTOR_NOT_FOUND),
        ("synthetic", "missingOperation", PolicyReasonCode.OPERATION_NOT_FOUND),
    ],
)
def test_blocks_unknown_catalog_entries(
    catalog: ConnectorCatalog,
    connector_id: str,
    operation_id: str,
    expected_code: PolicyReasonCode,
) -> None:
    """Somente identificadores resolvidos no catálogo entram nas regras seguintes."""

    request = PolicyEvaluationRequest(
        execution=_execution(connector_id=connector_id, operation_id=operation_id)
    )

    decision = PolicyEngine(catalog).evaluate(request)

    assert decision.outcome is PolicyOutcome.BLOCK
    assert decision.reason_codes == [expected_code]


def test_blocks_disabled_operation(tmp_path: Path) -> None:
    """Uma operação presente no OpenAPI continua opt-in no profile."""

    catalog = _temporary_policy_catalog(tmp_path, enabled=False)
    request = PolicyEvaluationRequest(
        execution=_execution(
            connector_id="nested",
            operation_id="changeItem",
            path={"itemId": "item-1"},
        )
    )

    decision = PolicyEngine(catalog).evaluate(request)

    assert decision.reason_codes == [PolicyReasonCode.OPERATION_DISABLED]


def test_blocks_missing_permission_and_indirect_request(catalog: ConnectorCatalog) -> None:
    """Permissão confiável e pedido humano direto são controles independentes e cumulativos."""

    request = _synthetic_write_request(
        principal=PolicyPrincipal(id="user-1"),
        direct_request=False,
    )

    decision = PolicyEngine(catalog).evaluate(request)

    assert decision.outcome is PolicyOutcome.BLOCK
    assert decision.reason_codes == [
        PolicyReasonCode.PERMISSION_DENIED,
        PolicyReasonCode.DIRECT_REQUEST_REQUIRED,
    ]


@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        ({"status": "inactive"}, PolicyReasonCode.JUSTIFICATION_REQUIRED),
        (
            {"status": "inactive", "justification": "      "},
            PolicyReasonCode.JUSTIFICATION_REQUIRED,
        ),
        (
            {"status": "inactive", "justification": "curta"},
            PolicyReasonCode.JUSTIFICATION_TOO_SHORT,
        ),
    ],
)
def test_validates_trimmed_justification(
    catalog: ConnectorCatalog,
    body: dict[str, str],
    expected_code: PolicyReasonCode,
) -> None:
    """A contagem ignora espaços externos e não aceita conteúdo composto apenas por whitespace."""

    decision = PolicyEngine(catalog).evaluate(_synthetic_write_request(body=body))

    assert decision.outcome is PolicyOutcome.BLOCK
    assert expected_code in decision.reason_codes


def test_reads_justification_from_configurable_nested_pointer(tmp_path: Path) -> None:
    """Uma API com outro formato de body muda apenas YAML, não o Python."""

    catalog = _temporary_policy_catalog(tmp_path)
    request = PolicyEvaluationRequest(
        execution=_execution(
            connector_id="nested",
            operation_id="changeItem",
            path={"itemId": "item-1"},
            body={"metadata": {"reasons": [{"text": "motivo suficiente"}]}},
        ),
        direct_request=True,
    )

    decision = PolicyEngine(catalog).evaluate(request)

    assert decision.outcome is PolicyOutcome.SIMULATE


@pytest.mark.parametrize(
    ("principal_scopes", "resource_scopes", "context_company"),
    [
        ({}, {"company_id": "company-1"}, "company-1"),
        ({"company_id": "company-1"}, {}, "company-1"),
        ({"company_id": "company-1"}, {"company_id": "company-1"}, None),
    ],
)
def test_blocks_when_any_required_scope_source_is_missing(
    catalog: ConnectorCatalog,
    principal_scopes: Mapping[str, Any],
    resource_scopes: Mapping[str, Any],
    context_company: Any,
) -> None:
    """Principal, evidência do recurso e contexto precisam fornecer todos os escopos declarados."""

    decision = PolicyEngine(catalog).evaluate(
        _tractian_write_request(
            principal_scopes=principal_scopes,
            resource_scopes=resource_scopes,
            context_company=context_company,
        )
    )

    assert decision.outcome is PolicyOutcome.BLOCK
    assert PolicyReasonCode.REQUIRED_SCOPE_MISSING in decision.reason_codes


def test_blocks_divergent_scope_and_accepts_exact_match(catalog: ConnectorCatalog) -> None:
    """Valores iguais com tipos diferentes também divergem, evitando coerção ambígua de tenant."""

    divergent = PolicyEngine(catalog).evaluate(
        _tractian_write_request(resource_scopes={"company_id": "company-2"})
    )
    exact = PolicyEngine(catalog).evaluate(_tractian_write_request())

    assert PolicyReasonCode.SCOPE_MISMATCH in divergent.reason_codes
    assert exact.outcome is PolicyOutcome.SIMULATE


def test_digest_is_stable_and_binds_action_principal_and_context(
    catalog: ConnectorCatalog,
) -> None:
    """Qualquer sinal relevante diferente invalida a confirmação sem revelar seu conteúdo."""

    engine = PolicyEngine(catalog)
    base = _synthetic_write_request(context={"session": "one"})
    reordered_body = {
        "justification": "manutenção preventiva solicitada pela equipe",
        "status": "inactive",
    }
    same = _synthetic_write_request(body=reordered_body, context={"session": "one"})
    changed_action = _synthetic_write_request(
        body={
            "status": "active",
            "justification": "manutenção preventiva solicitada pela equipe",
        },
        context={"session": "one"},
    )
    changed_principal = _synthetic_write_request(
        principal=PolicyPrincipal(id="user-2", permissions=["action_high"]),
        context={"session": "one"},
    )
    changed_context = _synthetic_write_request(context={"session": "two"})
    changed_resource_scope = base.model_copy(update={"resource_scopes": {"tenant_id": "tenant-2"}})

    digests = [
        engine.evaluate(candidate).action_digest
        for candidate in (
            base,
            same,
            changed_action,
            changed_principal,
            changed_context,
            changed_resource_scope,
        )
    ]

    assert digests[0] == digests[1]
    assert len(digests[0] or "") == 64
    assert len(set(digests)) == 5


def test_simulates_high_risk_write_without_confirmation(catalog: ConnectorCatalog) -> None:
    """Simular é seguro; a decisão apenas avisa que executar exigiria confirmação posterior."""

    decision = PolicyEngine(catalog, execution_mode="simulate").evaluate(_synthetic_write_request())

    assert decision.outcome is PolicyOutcome.SIMULATE
    assert decision.confirmation_required_for_execute is True
    assert decision.action_digest is not None


def test_real_write_requires_confirmation_then_remains_disabled(
    catalog: ConnectorCatalog,
) -> None:
    """Mesmo uma confirmação válida não habilita mutações no incremento atual."""

    engine = PolicyEngine(catalog, execution_mode="execute")
    initial = _synthetic_write_request()
    pending = engine.evaluate(initial)
    assert pending.outcome is PolicyOutcome.REQUIRE_CONFIRMATION
    assert pending.reason_codes == [PolicyReasonCode.CONFIRMATION_REQUIRED]

    wrong_person = engine.evaluate(
        _synthetic_write_request(
            confirmation=PolicyConfirmation(
                confirmed_by="user-2",
                action_digest=pending.action_digest,
            )
        )
    )
    wrong_action = engine.evaluate(
        _synthetic_write_request(
            confirmation=PolicyConfirmation(
                confirmed_by="user-1",
                action_digest="0" * 64,
            )
        )
    )
    valid = engine.evaluate(
        _synthetic_write_request(
            confirmation=PolicyConfirmation(
                confirmed_by="user-1",
                action_digest=pending.action_digest,
            )
        )
    )

    assert wrong_person.reason_codes == [PolicyReasonCode.CONFIRMATION_MISMATCH]
    assert wrong_action.reason_codes == [PolicyReasonCode.CONFIRMATION_MISMATCH]
    assert valid.outcome is PolicyOutcome.BLOCK
    assert valid.reason_codes == [PolicyReasonCode.REAL_WRITE_DISABLED]


def test_invalid_non_json_action_is_blocked(catalog: ConnectorCatalog) -> None:
    """O digest falha fechado quando recebe um objeto que não pertence a uma API REST JSON."""

    request = _synthetic_write_request(body={"justification": "x" * 25, "value": object()})

    decision = PolicyEngine(catalog).evaluate(request)

    assert decision.reason_codes == [PolicyReasonCode.INVALID_ACTION_ARGUMENTS]


def test_decision_does_not_expose_raw_policy_inputs(catalog: ConnectorCatalog) -> None:
    """Trace e UI recebem metadados e hash, não justificativa, claims nem ids de escopo."""

    secret_reason = "justificativa operacional que não deve sair na decisão"
    decision = PolicyEngine(catalog).evaluate(
        _synthetic_write_request(
            body={"status": "inactive", "justification": secret_reason},
            principal=PolicyPrincipal(
                id="private-user",
                permissions=["action_high"],
                scopes={"private_scope": "private-value"},
            ),
        )
    )
    serialized = decision.model_dump_json()

    assert secret_reason not in serialized
    assert "private-user" not in serialized
    assert "private-value" not in serialized


def _run_guarded(
    catalog: ConnectorCatalog,
    request: PolicyEvaluationRequest,
    *,
    execution_mode: str = "simulate",
) -> tuple[Any, int]:
    """Executa a composição completa com transporte em memória e contabiliza chamadas HTTP."""

    network_calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(200, json={"id": "widget-1", "status": "active"})

    async def run() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            http_executor = HttpExecutor(
                catalog,
                environment={"SYNTHETIC_API_URL": "http://localhost:9000"},
                client=client,
                execution_mode=execution_mode,
            )
            guarded = GuardedExecutor(
                PolicyEngine(catalog, execution_mode=execution_mode),
                http_executor,
            )
            return await guarded.execute(request)

    return asyncio.run(run()), network_calls


def test_guarded_executor_executes_read_and_simulates_write(catalog: ConnectorCatalog) -> None:
    """Somente os outcomes allow/simulate atravessam a fronteira para o HttpExecutor."""

    read_request = PolicyEvaluationRequest(
        execution=_execution(
            operation_id="getWidget",
            path={"widgetId": "widget-1"},
            body=_NO_BODY,
        )
    )
    read_result, read_calls = _run_guarded(catalog, read_request)
    write_result, write_calls = _run_guarded(catalog, _synthetic_write_request())

    assert read_result.policy.outcome is PolicyOutcome.ALLOW
    assert read_result.execution.outcome is ExecutionOutcome.EXECUTED
    assert read_calls == 1
    assert write_result.policy.outcome is PolicyOutcome.SIMULATE
    assert write_result.execution.outcome is ExecutionOutcome.SIMULATED
    assert write_result.execution.attempts == 0
    assert write_calls == 0


def test_guarded_executor_never_calls_http_when_blocked_or_awaiting_confirmation(
    catalog: ConnectorCatalog,
) -> None:
    """Bloqueio e confirmação pendente encerram o fluxo antes do executor."""

    blocked, blocked_calls = _run_guarded(
        catalog,
        _synthetic_write_request(principal=PolicyPrincipal(id="user-1")),
    )
    pending, pending_calls = _run_guarded(
        catalog,
        _synthetic_write_request(),
        execution_mode="execute",
    )

    assert blocked.policy.outcome is PolicyOutcome.BLOCK
    assert blocked.execution is None
    assert blocked_calls == 0
    assert pending.policy.outcome is PolicyOutcome.REQUIRE_CONFIRMATION
    assert pending.execution is None
    assert pending_calls == 0


def test_guarded_executor_rejects_divergent_execution_modes(catalog: ConnectorCatalog) -> None:
    """Uma configuração incoerente falha no startup da composição, nunca durante uma ação."""

    executor = HttpExecutor(catalog, execution_mode="simulate")

    with pytest.raises(ValueError, match="mesmo execution_mode"):
        GuardedExecutor(PolicyEngine(catalog, execution_mode="execute"), executor)
