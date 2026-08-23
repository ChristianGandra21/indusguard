"""Policy engine determinística executada antes de qualquer acesso HTTP.

O modelo de linguagem poderá propor uma operação, mas não decide se ela é segura. Este módulo
usa apenas sinais tipados e confiáveis: o profile do conector, a identidade autenticada, suas
permissões, o escopo comprovado do recurso e a confirmação humana vinculada à ação exata.

Não há regras da Tractian aqui. Toda particularidade de uma integração chega pelo catálogo,
construído a partir de OpenAPI + ``profile.yaml``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Final

from indusguard_api.connectors import ConnectorCatalog, ResolvedOperation
from indusguard_api.executor import HttpExecutor
from indusguard_api.observability import NoOpTelemetry, Telemetry
from indusguard_api.schemas import (
    AccessMode,
    ExecutionMode,
    GuardedExecutionResult,
    PolicyDecision,
    PolicyEvaluationRequest,
    PolicyOutcome,
    PolicyReasonCode,
)

_MISSING: Final = object()


def _same_claim(left: Any, right: Any) -> bool:
    """Compara claims sem coerção: ``1`` não representa o mesmo escopo que ``"1"``."""

    return type(left) is type(right) and left == right


def _append_once(codes: list[PolicyReasonCode], code: PolicyReasonCode) -> None:
    """Mantém códigos em ordem determinística sem repetir a mesma causa."""

    if code not in codes:
        codes.append(code)


def _resolve_json_pointer(document: Any, pointer: str) -> Any:
    """Lê um JSON Pointer RFC 6901 e devolve um sentinela quando o caminho não existe."""

    if pointer == "":
        return document

    current = document
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                return _MISSING
            current = current[token]
            continue
        # String também é Sequence, portanto a exclusão explícita evita tratá-la como lista JSON.
        if isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray)):
            if not token.isdigit():
                return _MISSING
            index = int(token)
            if index >= len(current):
                return _MISSING
            current = current[index]
            continue
        return _MISSING
    return current


class PolicyEngine:
    """Avalia uma operação conhecida sem rede, banco ou decisão probabilística."""

    def __init__(
        self,
        catalog: ConnectorCatalog,
        *,
        execution_mode: ExecutionMode = "simulate",
        telemetry: Telemetry | None = None,
    ) -> None:
        if execution_mode not in {"simulate", "execute"}:
            raise ValueError("execution_mode precisa ser 'simulate' ou 'execute'")
        self._catalog = catalog
        self._execution_mode = execution_mode
        self._telemetry = telemetry or NoOpTelemetry()

    @property
    def execution_mode(self) -> ExecutionMode:
        """Modo imutável usado também pelo ``GuardedExecutor`` para detectar configuração errada."""

        return self._execution_mode

    @property
    def telemetry(self) -> Telemetry:
        """Permite que o GuardedExecutor herde o mesmo trace sem acessar exportadores."""

        return self._telemetry

    def evaluate(self, request: PolicyEvaluationRequest) -> PolicyDecision:
        """Instrumenta a decisão sem registrar argumentos, contexto ou identidade."""

        with self._telemetry.start_span(
            "indusguard.policy.evaluate",
            {
                "indusguard.connector.id": request.execution.connector_id,
                "indusguard.operation.id": request.execution.operation_id,
                "indusguard.execution.mode": self._execution_mode,
            },
        ) as span:
            decision = self._evaluate_untraced(request)
            span.set_attribute("indusguard.policy.outcome", decision.outcome.value)
            span.set_attribute(
                "indusguard.policy.reason_codes",
                tuple(code.value for code in decision.reason_codes),
            )
            span.set_attribute(
                "indusguard.policy.confirmation_required",
                decision.confirmation_required_for_execute,
            )
            if decision.access:
                span.set_attribute("indusguard.operation.access", decision.access.value)
            if decision.risk:
                span.set_attribute("indusguard.operation.risk", decision.risk.value)
            # Um bloqueio é a policy funcionando corretamente, não uma falha técnica do span.
            span.set_attribute(
                "indusguard.policy.blocked",
                decision.outcome is PolicyOutcome.BLOCK,
            )
            return decision

    def _evaluate_untraced(self, request: PolicyEvaluationRequest) -> PolicyDecision:
        """Produz uma decisão reproduzível e segura para auditoria."""

        execution = request.execution
        if self._catalog.get(execution.connector_id) is None:
            return self._decision(
                request,
                outcome=PolicyOutcome.BLOCK,
                reason_codes=[PolicyReasonCode.CONNECTOR_NOT_FOUND],
                message="O conector solicitado não existe no catálogo validado.",
            )

        resolved = self._catalog.resolve_operation(
            execution.connector_id,
            execution.operation_id,
        )
        if resolved is None:
            return self._decision(
                request,
                outcome=PolicyOutcome.BLOCK,
                reason_codes=[PolicyReasonCode.OPERATION_NOT_FOUND],
                message="A operação solicitada não existe no conector.",
            )
        if not resolved.operation.enabled:
            return self._decision(
                request,
                resolved=resolved,
                outcome=PolicyOutcome.BLOCK,
                reason_codes=[PolicyReasonCode.OPERATION_DISABLED],
                message="A operação está desabilitada pelo profile do conector.",
            )

        action_digest: str | None = None
        if resolved.operation.access is AccessMode.WRITE:
            action_digest = self._action_digest(request)
            if action_digest is None:
                return self._decision(
                    request,
                    resolved=resolved,
                    outcome=PolicyOutcome.BLOCK,
                    reason_codes=[PolicyReasonCode.INVALID_ACTION_ARGUMENTS],
                    message="Os argumentos da ação não podem ser representados como JSON seguro.",
                )

        blocking_reasons = self._authorization_reasons(request, resolved)
        if blocking_reasons:
            return self._decision(
                request,
                resolved=resolved,
                outcome=PolicyOutcome.BLOCK,
                reason_codes=blocking_reasons,
                action_digest=action_digest,
                message="A política determinística bloqueou a operação por sinais insuficientes.",
            )

        if resolved.operation.access is AccessMode.READ:
            return self._decision(
                request,
                resolved=resolved,
                outcome=PolicyOutcome.ALLOW,
                reason_codes=[PolicyReasonCode.READ_APPROVED],
                message="A leitura atende às regras determinísticas do conector.",
            )

        if self._execution_mode == "simulate":
            return self._decision(
                request,
                resolved=resolved,
                outcome=PolicyOutcome.SIMULATE,
                reason_codes=[PolicyReasonCode.WRITE_SIMULATION_APPROVED],
                action_digest=action_digest,
                message="A escrita pode ser validada e exibida, mas não será enviada à API.",
            )

        # Confirmação só faz sentido para execução real. Ela vincula a pessoa ao digest gerado
        # sobre a ação completa e evita reaproveitar um aceite em argumentos diferentes.
        if resolved.operation.requires_confirmation:
            confirmation = request.confirmation
            if confirmation is None:
                return self._decision(
                    request,
                    resolved=resolved,
                    outcome=PolicyOutcome.REQUIRE_CONFIRMATION,
                    reason_codes=[PolicyReasonCode.CONFIRMATION_REQUIRED],
                    action_digest=action_digest,
                    message="A execução real exige confirmação vinculada a esta ação.",
                )
            if (
                request.principal is None
                or confirmation.confirmed_by != request.principal.id
                or confirmation.action_digest != action_digest
            ):
                return self._decision(
                    request,
                    resolved=resolved,
                    outcome=PolicyOutcome.REQUIRE_CONFIRMATION,
                    reason_codes=[PolicyReasonCode.CONFIRMATION_MISMATCH],
                    action_digest=action_digest,
                    message="A confirmação não corresponde à pessoa ou à ação atual.",
                )

        # Este bloqueio é intencional e permanece mesmo depois de uma confirmação válida. O
        # incremento atual prova a decisão; habilitar mutação real exigirá outro gate de release.
        return self._decision(
            request,
            resolved=resolved,
            outcome=PolicyOutcome.BLOCK,
            reason_codes=[PolicyReasonCode.REAL_WRITE_DISABLED],
            action_digest=action_digest,
            message="Escritas reais permanecem desabilitadas neste incremento.",
        )

    def _authorization_reasons(
        self,
        request: PolicyEvaluationRequest,
        resolved: ResolvedOperation,
    ) -> list[PolicyReasonCode]:
        """Valida identidade, escopos, permissão, intenção direta e justificativa."""

        reasons: list[PolicyReasonCode] = []
        principal = request.principal
        operation = resolved.operation

        if resolved.profile.auth.type == "context_header":
            context_field = resolved.profile.auth.context_field
            if principal is None:
                _append_once(reasons, PolicyReasonCode.PRINCIPAL_REQUIRED)
            elif context_field is None or not _same_claim(
                request.execution.context.get(context_field, _MISSING),
                principal.id,
            ):
                _append_once(reasons, PolicyReasonCode.PRINCIPAL_CONTEXT_MISMATCH)

        if operation.required_scopes and principal is None:
            _append_once(reasons, PolicyReasonCode.PRINCIPAL_REQUIRED)

        for scope in operation.required_scopes:
            principal_value = principal.scopes.get(scope, _MISSING) if principal else _MISSING
            resource_value = request.resource_scopes.get(scope, _MISSING)
            context_value = request.execution.context.get(scope, _MISSING)
            values = (principal_value, resource_value, context_value)
            if _MISSING in values:
                _append_once(reasons, PolicyReasonCode.REQUIRED_SCOPE_MISSING)
            elif not (
                _same_claim(principal_value, resource_value)
                and _same_claim(resource_value, context_value)
            ):
                _append_once(reasons, PolicyReasonCode.SCOPE_MISMATCH)

        if operation.permission and (
            principal is None or operation.permission not in principal.permissions
        ):
            _append_once(reasons, PolicyReasonCode.PERMISSION_DENIED)

        if operation.requires_direct_request and not request.direct_request:
            _append_once(reasons, PolicyReasonCode.DIRECT_REQUEST_REQUIRED)

        if operation.justification_min_length:
            justification = _resolve_json_pointer(
                request.execution.arguments.body,
                operation.justification_pointer,
            )
            if not isinstance(justification, str) or not justification.strip():
                _append_once(reasons, PolicyReasonCode.JUSTIFICATION_REQUIRED)
            elif len(justification.strip()) < operation.justification_min_length:
                _append_once(reasons, PolicyReasonCode.JUSTIFICATION_TOO_SHORT)

        return reasons

    @staticmethod
    def _action_digest(request: PolicyEvaluationRequest) -> str | None:
        """Gera SHA-256 canônico; somente o hash, nunca os dados brutos, sai na decisão."""

        principal = request.principal
        payload = {
            "connector_id": request.execution.connector_id,
            "operation_id": request.execution.operation_id,
            "arguments": request.execution.arguments.model_dump(),
            "context": request.execution.context,
            "principal": (
                {
                    "id": principal.id,
                    "permissions": sorted(principal.permissions),
                    "scopes": principal.scopes,
                }
                if principal
                else None
            ),
            "resource_scopes": request.resource_scopes,
        }
        try:
            canonical = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            return None
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _decision(
        request: PolicyEvaluationRequest,
        *,
        outcome: PolicyOutcome,
        reason_codes: list[PolicyReasonCode],
        message: str,
        resolved: ResolvedOperation | None = None,
        action_digest: str | None = None,
    ) -> PolicyDecision:
        """Centraliza o envelope para que todos os caminhos exponham os mesmos metadados."""

        operation = resolved.operation if resolved else None
        return PolicyDecision(
            connector_id=request.execution.connector_id,
            operation_id=request.execution.operation_id,
            outcome=outcome,
            reason_codes=reason_codes,
            access=operation.access if operation else None,
            risk=operation.risk if operation else None,
            required_permission=operation.permission if operation else None,
            required_scopes=operation.required_scopes if operation else [],
            confirmation_required_for_execute=(
                bool(operation.requires_confirmation)
                if operation and operation.access is AccessMode.WRITE
                else False
            ),
            action_digest=action_digest,
            message=message,
        )


class GuardedExecutor:
    """Única composição autorizada a encaminhar uma decisão política ao executor HTTP."""

    def __init__(
        self,
        policy_engine: PolicyEngine,
        http_executor: HttpExecutor,
        *,
        telemetry: Telemetry | None = None,
    ) -> None:
        if policy_engine.execution_mode != http_executor.execution_mode:
            raise ValueError("PolicyEngine e HttpExecutor precisam usar o mesmo execution_mode")
        self._policy_engine = policy_engine
        self._http_executor = http_executor
        self._telemetry = telemetry or policy_engine.telemetry

    async def execute(self, request: PolicyEvaluationRequest) -> GuardedExecutionResult:
        """Chama HTTP só para leitura permitida ou escrita aprovada para simulação."""

        with self._telemetry.start_span(
            "indusguard.action",
            {
                "indusguard.connector.id": request.execution.connector_id,
                "indusguard.operation.id": request.execution.operation_id,
            },
        ) as span:
            decision = self._policy_engine.evaluate(request)
            span.set_attribute("indusguard.policy.outcome", decision.outcome.value)
            if decision.outcome not in {PolicyOutcome.ALLOW, PolicyOutcome.SIMULATE}:
                span.set_attribute("indusguard.action.executed", False)
                return GuardedExecutionResult(policy=decision)

            execution = await self._http_executor.execute(request.execution)
            span.set_attribute("indusguard.action.executed", execution.attempts > 0)
            span.set_attribute("indusguard.execution.outcome", execution.outcome.value)
            return GuardedExecutionResult(policy=decision, execution=execution)
