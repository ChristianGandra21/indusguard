"""Probe sintético e auditável dos contratos exigidos dos fallbacks do piloto."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Any

from indusguard_api.agent import (
    AgentModelError,
    AgentModelGateway,
    AgentPlanningContext,
    AgentRunRequest,
    AgentToolDefinition,
    GatewayResult,
    ModelOutputError,
    ModelRateLimitedError,
    ModelUnavailableError,
    TokenUsage,
)
from indusguard_api.schemas import ConnectorDomain, DomainIntent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import BaseModel, ConfigDict, Field

from indusguard_evals.pilot_models import (
    OpenAICompatibleAgentModelGateway,
    OpenAICompatibleProviderConfig,
    PilotFallbackProvider,
)

PROVIDER_PROBE_SCHEMA_VERSION = "provider-compatibility-probe-v1"
SYNTHETIC_PROBE_VERSION = "fallback-contracts-v1"
PROBE_EVIDENCE_ID = "ev-provider-probe"
PROBE_TOOL_ALIAS = "tractian__getAsset"


class ProviderProbeStage(StrEnum):
    """Contrato externo que estava sendo exercitado quando o probe terminou."""

    CLASSIFY = "classify"
    PLAN = "plan"
    FINALIZE = "finalize"
    COMPLETE = "complete"


class ProviderProbeStatus(StrEnum):
    """Resultado binário usado como gate do manifesto."""

    PASSED = "passed"
    FAILED = "failed"


class ProviderProbeResult(BaseModel):
    """Resultado redigido de um provedor, sem prompts, respostas ou credenciais."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: PilotFallbackProvider
    model: str
    status: ProviderProbeStatus
    stage: ProviderProbeStage
    reason_code: str | None = None
    model_calls: int = Field(ge=0, le=3)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    retry_after_seconds: int | None = Field(default=None, ge=0, le=86_400)


class ProviderProbeReport(BaseModel):
    """Artefato que vincula compatibilidade observada ao manifesto autorizado."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = PROVIDER_PROBE_SCHEMA_VERSION
    synthetic_probe_version: str = SYNTHETIC_PROBE_VERSION
    created_at: datetime
    git_commit: str = Field(min_length=1)
    preflight_manifest_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    transmitted_categories: list[str]
    excluded_categories: list[str]
    results: list[ProviderProbeResult]
    all_compatible: bool
    report_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


def _probe_domain() -> ConnectorDomain:
    return ConnectorDomain(
        id="tractian",
        language="pt-BR",
        context_fields=["asset_id"],
        terminology={"asset": "ativo industrial monitorado"},
        intents=[
            DomainIntent(
                id="investigar",
                description="Investigar a condição de um ativo usando evidência observada.",
                evidence_operations=["getAsset"],
            )
        ],
        evidence_states=["complete"],
    )


def _probe_tool() -> AgentToolDefinition:
    return AgentToolDefinition(
        alias=PROBE_TOOL_ALIAS,
        mcp_name="tractian.getAsset",
        description="Consulta o ativo solicitado e retorna sua condição observada.",
        input_schema={
            "type": "object",
            "properties": {"asset_id": {"type": "string", "const": "asset-probe"}},
            "required": ["asset_id"],
            "additionalProperties": False,
        },
        read_only=True,
        destructive=False,
        idempotent=True,
    )


def _usage_sum(current: TokenUsage, result: GatewayResult[Any]) -> TokenUsage:
    return TokenUsage(
        input_tokens=current.input_tokens + result.usage.input_tokens,
        output_tokens=current.output_tokens + result.usage.output_tokens,
    )


def _failure(
    config: OpenAICompatibleProviderConfig,
    *,
    stage: ProviderProbeStage,
    reason_code: str,
    model_calls: int,
    usage: TokenUsage,
    retry_after_seconds: int | None = None,
) -> ProviderProbeResult:
    return ProviderProbeResult(
        provider=config.provider,
        model=config.model,
        status=ProviderProbeStatus.FAILED,
        stage=stage,
        reason_code=reason_code,
        model_calls=model_calls,
        usage=usage,
        retry_after_seconds=retry_after_seconds,
    )


def _failure_from_error(
    config: OpenAICompatibleProviderConfig,
    error: AgentModelError,
    *,
    stage: ProviderProbeStage,
    model_calls: int,
    usage: TokenUsage,
) -> ProviderProbeResult:
    if isinstance(error, ModelRateLimitedError):
        return _failure(
            config,
            stage=stage,
            reason_code="MODEL_RATE_LIMITED",
            model_calls=model_calls,
            usage=usage,
            retry_after_seconds=error.retry_after_seconds,
        )
    if isinstance(error, ModelUnavailableError):
        return _failure(
            config,
            stage=stage,
            reason_code=error.reason_code,
            model_calls=model_calls,
            usage=usage,
        )
    if isinstance(error, ModelOutputError):
        return _failure(
            config,
            stage=stage,
            reason_code="MODEL_OUTPUT_INVALID",
            model_calls=model_calls,
            usage=usage,
        )
    return _failure(
        config,
        stage=stage,
        reason_code="MODEL_UNAVAILABLE",
        model_calls=model_calls,
        usage=usage,
    )


async def probe_provider(
    config: OpenAICompatibleProviderConfig,
    gateway: AgentModelGateway,
) -> ProviderProbeResult:
    """Exercita os três contratos do runtime usando somente um ativo sintético."""

    request = AgentRunRequest(
        connector_id="tractian",
        message=(
            "Investigue o ativo sintético asset-probe. Consulte obrigatoriamente a única "
            "ferramenta disponível e finalize citando a evidência retornada."
        ),
        seed=42,
    )
    domain = _probe_domain()
    context = AgentPlanningContext(context={"asset_id": "asset-probe"})
    tool = _probe_tool()
    messages = [HumanMessage(content=request.message)]
    usage = TokenUsage()
    calls = 0

    try:
        calls += 1
        classification = await gateway.classify(request=request, domain=domain)
        usage = _usage_sum(usage, classification)
    except AgentModelError as exc:
        return _failure_from_error(
            config,
            exc,
            stage=ProviderProbeStage.CLASSIFY,
            model_calls=calls,
            usage=usage,
        )
    if classification.value.intent_id != "investigar":
        return _failure(
            config,
            stage=ProviderProbeStage.CLASSIFY,
            reason_code="MODEL_CLASSIFICATION_CONTRACT_INVALID",
            model_calls=calls,
            usage=usage,
        )

    try:
        calls += 1
        plan = await gateway.plan(
            request=request,
            domain=domain,
            intent=classification.value,
            planning_context=context,
            messages=messages,
            tools=[tool],
        )
        usage = _usage_sum(usage, plan)
    except AgentModelError as exc:
        return _failure_from_error(
            config,
            exc,
            stage=ProviderProbeStage.PLAN,
            model_calls=calls,
            usage=usage,
        )
    tool_calls = plan.value.tool_calls
    if (
        len(tool_calls) != 1
        or tool_calls[0].alias != PROBE_TOOL_ALIAS
        or tool_calls[0].arguments != {"asset_id": "asset-probe"}
    ):
        return _failure(
            config,
            stage=ProviderProbeStage.PLAN,
            reason_code="MODEL_TOOL_CONTRACT_INVALID",
            model_calls=calls,
            usage=usage,
        )

    planned = tool_calls[0]
    provider_message = plan.provider_message or AIMessage(
        content="",
        tool_calls=[
            {
                "name": planned.alias,
                "args": planned.arguments,
                "id": planned.call_id,
                "type": "tool_call",
            }
        ],
    )
    messages.extend(
        [
            provider_message,
            ToolMessage(
                content=json.dumps(
                    {
                        "evidence_id": PROBE_EVIDENCE_ID,
                        "execution": {
                            "outcome": "success",
                            "data": {"asset_id": "asset-probe", "condition": "normal"},
                        },
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                tool_call_id=planned.call_id,
                name=planned.alias,
            ),
        ]
    )
    try:
        calls += 1
        final = await gateway.finalize(
            request=request,
            domain=domain,
            intent=classification.value,
            planning_context=context,
            messages=messages,
            allowed_evidence_ids=[PROBE_EVIDENCE_ID],
        )
        usage = _usage_sum(usage, final)
    except AgentModelError as exc:
        return _failure_from_error(
            config,
            exc,
            stage=ProviderProbeStage.FINALIZE,
            model_calls=calls,
            usage=usage,
        )
    if final.value.evidence_ids != [PROBE_EVIDENCE_ID]:
        return _failure(
            config,
            stage=ProviderProbeStage.FINALIZE,
            reason_code="MODEL_EVIDENCE_CONTRACT_INVALID",
            model_calls=calls,
            usage=usage,
        )
    return ProviderProbeResult(
        provider=config.provider,
        model=config.model,
        status=ProviderProbeStatus.PASSED,
        stage=ProviderProbeStage.COMPLETE,
        model_calls=calls,
        usage=usage,
    )


def _report_digest(report: ProviderProbeReport) -> str:
    payload = report.model_dump(mode="json", exclude={"report_digest"})
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


async def build_provider_probe_report(
    configs: Sequence[OpenAICompatibleProviderConfig],
    *,
    git_commit: str,
    preflight_manifest_digest: str,
    gateway_factory: Callable[[OpenAICompatibleProviderConfig], AgentModelGateway] | None = None,
) -> ProviderProbeReport:
    """Executa provedores em ordem e conserva apenas metadados seguros."""

    if not configs:
        raise ValueError("provider probe exige ao menos um fallback configurado")
    factory = gateway_factory or OpenAICompatibleAgentModelGateway
    results = [await probe_provider(config, factory(config)) for config in configs]
    report = ProviderProbeReport(
        created_at=datetime.now(UTC),
        git_commit=git_commit,
        preflight_manifest_digest=preflight_manifest_digest,
        transmitted_categories=[
            "fixed_synthetic_request",
            "fixed_synthetic_domain",
            "fixed_synthetic_tool_schema",
            "fixed_synthetic_tool_result",
        ],
        excluded_categories=[
            "pilot_tickets",
            "goldens",
            "credentials",
            "real_connector_payloads",
            "provider_response_bodies",
        ],
        results=results,
        all_compatible=all(item.status is ProviderProbeStatus.PASSED for item in results),
        report_digest="0" * 64,
    )
    return report.model_copy(update={"report_digest": _report_digest(report)})


def write_provider_probe_report(path: Path, report: ProviderProbeReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")


def load_and_validate_provider_probe(
    path: Path,
    *,
    git_commit: str,
    preflight_manifest_digest: str,
    expected_providers: Sequence[tuple[str, str]],
) -> ProviderProbeReport:
    """Recusa probe adulterado, reusado em outro commit/configuração ou incompleto."""

    try:
        report = ProviderProbeReport.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("PROVIDER_PROBE_INVALID: artefato ausente ou inválido") from exc
    if report.report_digest != _report_digest(report):
        raise ValueError("PROVIDER_PROBE_INVALID: digest divergente")
    observed = [(item.provider.value, item.model) for item in report.results]
    if (
        report.git_commit != git_commit
        or report.preflight_manifest_digest != preflight_manifest_digest
        or observed != list(expected_providers)
    ):
        raise ValueError("PROVIDER_PROBE_STALE: commit, manifesto ou provedores divergentes")
    if not report.all_compatible or any(
        item.status is not ProviderProbeStatus.PASSED for item in report.results
    ):
        raise ValueError("PROVIDER_PROBE_FAILED: ao menos um fallback não cumpre os contratos")
    return report


__all__ = [
    "PROVIDER_PROBE_SCHEMA_VERSION",
    "ProviderProbeReport",
    "ProviderProbeResult",
    "ProviderProbeStage",
    "ProviderProbeStatus",
    "build_provider_probe_report",
    "load_and_validate_provider_probe",
    "probe_provider",
    "write_provider_probe_report",
]
