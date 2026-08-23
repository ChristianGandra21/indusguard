"""Baseline de avaliação que remove somente o gate determinístico.

Este módulo pertence ao pacote ``indusguard-evals`` e não é empacotado com a API. Ele preserva
catálogo, validação OpenAPI, autenticação e simulação do executor HTTP. A policy é aplicada depois
da run pelo scorer shadow, portanto nunca influencia a proposta observada nesta variante.
"""

from __future__ import annotations

from collections.abc import Callable

from indusguard_api.connectors import ConnectorCatalog
from indusguard_api.executor import HttpExecutor
from indusguard_api.schemas import (
    AccessMode,
    GuardedExecutionResult,
    PolicyDecision,
    PolicyEvaluationRequest,
    PolicyOutcome,
    PolicyReasonCode,
)


class PromptOnlyExecutor:
    """Executa a proposta sem autorização determinística, sempre em modo de simulação."""

    def __init__(
        self,
        catalog: ConnectorCatalog,
        http_executor: HttpExecutor,
        *,
        request_observer: Callable[[PolicyEvaluationRequest], None] | None = None,
    ) -> None:
        if http_executor.execution_mode != "simulate":
            raise ValueError("PromptOnlyExecutor exige HttpExecutor em modo simulate")
        self._catalog = catalog
        self._http_executor = http_executor
        self._request_observer = request_observer

    async def execute(self, request: PolicyEvaluationRequest) -> GuardedExecutionResult:
        """Produz o mesmo envelope do fluxo protegido sem usar a decisão shadow como gate."""

        resolved = self._catalog.resolve_operation(
            request.execution.connector_id,
            request.execution.operation_id,
        )
        if resolved is None or not resolved.operation.enabled:
            raise ValueError("a baseline recebeu uma operação ausente ou desabilitada")
        if self._request_observer is not None:
            self._request_observer(request.model_copy(deep=True))

        operation = resolved.operation
        outcome = (
            PolicyOutcome.ALLOW if operation.access is AccessMode.READ else PolicyOutcome.SIMULATE
        )
        marker = PolicyDecision(
            connector_id=request.execution.connector_id,
            operation_id=request.execution.operation_id,
            outcome=outcome,
            reason_codes=[
                PolicyReasonCode.READ_APPROVED
                if operation.access is AccessMode.READ
                else PolicyReasonCode.WRITE_SIMULATION_APPROVED
            ],
            access=operation.access,
            risk=operation.risk,
            required_permission=operation.permission,
            required_scopes=operation.required_scopes,
            confirmation_required_for_execute=(
                operation.access is AccessMode.WRITE and operation.requires_confirmation
            ),
            message="Baseline prompt-only: a policy será avaliada posteriormente em modo shadow.",
        )
        execution = await self._http_executor.execute(request.execution)
        return GuardedExecutionResult(policy=marker, execution=execution)
