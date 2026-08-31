"""Composição experimental sem abrir uma segunda porta de execução no produto."""

from __future__ import annotations

from collections.abc import Sequence

from indusguard_api.agent import (
    AgentModelGateway,
    AgentRunRecorder,
    AgentRunRequest,
    AgentRuntime,
    AgentRuntimeConfig,
    TrustedRunContext,
)
from indusguard_api.connectors import ConnectorCatalog
from indusguard_api.mcp_server import ProtectedOperationExecutor
from indusguard_api.policy import PolicyEngine
from indusguard_api.schemas import GuardedExecutionResult, PolicyEvaluationRequest, PolicyPrincipal

from indusguard_evals.contracts import (
    EvaluationCaseInput,
    EvaluationSample,
    EvaluationVariant,
    ScheduledRun,
    ShadowPolicyResult,
)
from indusguard_evals.tractian_fixture import store


class RecordingProtectedExecutor:
    """Observa a policy shadow no mesmo request e delega ao executor da variante."""

    def __init__(
        self,
        delegate: ProtectedOperationExecutor,
        shadow_policy: PolicyEngine,
    ) -> None:
        self._delegate = delegate
        self._shadow_policy = shadow_policy
        self._observations: list[ShadowPolicyResult] = []

    def reset(self) -> None:
        self._observations.clear()

    @property
    def observations(self) -> Sequence[ShadowPolicyResult]:
        return tuple(self._observations)

    async def execute(self, request: PolicyEvaluationRequest) -> GuardedExecutionResult:
        shadow = self._shadow_policy.evaluate(request)
        result = await self._delegate.execute(request)
        self._observations.append(
            ShadowPolicyResult(
                operation_id=request.execution.operation_id,
                outcome=shadow.outcome.value,
                reason_codes=[code.value for code in shadow.reason_codes],
                reached_executor=result.execution is not None,
            )
        )
        return result


def trusted_context_for_case(case: EvaluationCaseInput) -> TrustedRunContext:
    """Deriva claims da fixture, nunca da mensagem ou de argumentos escolhidos pelo modelo."""

    user = store.get_user(case.user_id)
    asset = store.get_asset(case.asset_id)
    if user is None or asset is None:
        raise ValueError(f"fixture incompleta para {case.case_id}")
    user_company = user.get("company_id")
    asset_company = asset.get("company_id")
    return TrustedRunContext(
        principal=PolicyPrincipal(
            id=case.user_id,
            permissions=[str(item) for item in user.get("permissions", [])],
            scopes={"company_id": user_company},
        ),
        execution_context={
            "user_id": case.user_id,
            "company_id": case.company_id,
            "asset_id": case.asset_id,
            "case_id": case.case_id,
        },
        resource_scopes={"company_id": asset_company},
        direct_request=case.direct_request,
    )


class VariantRuntime:
    """Executa uma identidade agendada e anexa observações que o modelo nunca recebeu."""

    def __init__(
        self,
        variant: EvaluationVariant,
        runtime: AgentRuntime,
        probe: RecordingProtectedExecutor,
    ) -> None:
        self.variant = variant
        self._runtime = runtime
        self._probe = probe

    async def run(
        self,
        scheduled: ScheduledRun,
        case: EvaluationCaseInput,
    ) -> EvaluationSample:
        if scheduled.variant is not self.variant:
            raise ValueError("schedule enviado ao runtime da variante errada")
        self._probe.reset()
        result = await self._runtime.run(
            AgentRunRequest(
                connector_id=case.connector_id,
                message=case.message,
                seed=scheduled.seed,
            ),
            trusted_context_for_case(case),
        )
        return EvaluationSample(
            scheduled=scheduled,
            result=result,
            shadow_policy=list(self._probe.observations),
        )


def create_variant_runtime(
    *,
    variant: EvaluationVariant,
    catalog: ConnectorCatalog,
    executor: ProtectedOperationExecutor,
    shadow_policy: PolicyEngine,
    model_gateway: AgentModelGateway,
    recorder: AgentRunRecorder | None = None,
    runtime_config: AgentRuntimeConfig | None = None,
) -> VariantRuntime:
    """Factory pequena garante que ambas as variantes usam o mesmo AgentRuntime e MCP."""

    probe = RecordingProtectedExecutor(executor, shadow_policy)
    runtime = AgentRuntime(
        catalog,
        probe,
        model_gateway,
        recorder=recorder,
        config=runtime_config,
    )
    return VariantRuntime(variant, runtime, probe)
