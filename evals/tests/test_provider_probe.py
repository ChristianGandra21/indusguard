"""O probe bloqueia fallbacks incompatíveis sem persistir conteúdo do provedor."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from indusguard_api.agent import (
    AgentDecision,
    AgentFinalAnswer,
    AgentIntentDecision,
    AgentPlannedToolCall,
    AgentPlanStep,
    GatewayResult,
    ModelOutputError,
    TokenUsage,
)
from langchain_core.messages import AIMessage
from pydantic import SecretStr

from indusguard_evals.pilot_models import (
    OpenAICompatibleProviderConfig,
    PilotFallbackProvider,
)
from indusguard_evals.provider_probe import (
    PROBE_EVIDENCE_ID,
    PROBE_TOOL_ALIAS,
    ProviderProbeStage,
    ProviderProbeStatus,
    build_provider_probe_report,
    load_and_validate_provider_probe,
    probe_provider,
    write_provider_probe_report,
)


def _config(
    provider: PilotFallbackProvider = PilotFallbackProvider.GEMINI,
) -> OpenAICompatibleProviderConfig:
    return OpenAICompatibleProviderConfig(
        provider=provider,
        base_url=f"https://{provider.value}.example/v1/",
        model=f"{provider.value}-model",
        api_key=SecretStr("must-not-be-serialized"),
        timeout_seconds=30,
        max_retries=0,
        max_tokens=2048,
        temperature=None,
        reasoning_effort="low" if provider is PilotFallbackProvider.GEMINI else None,
    )


class _ProbeGateway:
    model_name = "probe-model"

    def __init__(
        self,
        *,
        plan: AgentPlanStep | None = None,
        failure: Exception | None = None,
    ) -> None:
        self._plan = plan
        self._failure = failure

    async def classify(self, **_: Any) -> GatewayResult[AgentIntentDecision]:
        if self._failure is not None:
            raise self._failure
        return GatewayResult(
            AgentIntentDecision(intent_id="investigar"),
            TokenUsage(input_tokens=2, output_tokens=1),
        )

    async def plan(self, **_: Any) -> GatewayResult[AgentPlanStep]:
        step = self._plan or AgentPlanStep(
            tool_calls=[
                AgentPlannedToolCall(
                    alias=PROBE_TOOL_ALIAS,
                    arguments={"asset_id": "asset-probe"},
                    call_id="call-probe",
                )
            ]
        )
        return GatewayResult(
            step,
            TokenUsage(input_tokens=3, output_tokens=2),
            provider_message=AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": call.alias,
                        "args": call.arguments,
                        "id": call.call_id,
                        "type": "tool_call",
                    }
                    for call in step.tool_calls
                ],
            ),
        )

    async def finalize(self, **_: Any) -> GatewayResult[AgentFinalAnswer]:
        return GatewayResult(
            AgentFinalAnswer(
                answer="O ativo sintético está normal.",
                decision=AgentDecision.ORIENT,
                evidence_ids=[PROBE_EVIDENCE_ID],
            ),
            TokenUsage(input_tokens=5, output_tokens=3),
        )


def test_probe_passes_only_after_all_runtime_contracts() -> None:
    result = asyncio.run(probe_provider(_config(), _ProbeGateway()))

    assert result.status is ProviderProbeStatus.PASSED
    assert result.stage is ProviderProbeStage.COMPLETE
    assert result.model_calls == 3
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 6


def test_probe_redacts_structured_output_failure() -> None:
    result = asyncio.run(
        probe_provider(
            _config(PilotFallbackProvider.ELOAGENTS),
            _ProbeGateway(failure=ModelOutputError("raw provider body must not escape")),
        )
    )

    assert result.status is ProviderProbeStatus.FAILED
    assert result.stage is ProviderProbeStage.CLASSIFY
    assert result.reason_code == "MODEL_OUTPUT_INVALID"
    assert "provider body" not in result.model_dump_json()


@pytest.mark.parametrize(
    "plan",
    [
        AgentPlanStep(done=True),
        AgentPlanStep(
            tool_calls=[
                AgentPlannedToolCall(alias="view_skill", arguments={}, call_id="internal-call")
            ]
        ),
        AgentPlanStep(
            tool_calls=[
                AgentPlannedToolCall(
                    alias=PROBE_TOOL_ALIAS,
                    arguments={"asset_id": "invented"},
                    call_id="wrong-argument",
                )
            ]
        ),
    ],
)
def test_probe_rejects_missing_internal_or_untrusted_tool_calls(plan: AgentPlanStep) -> None:
    result = asyncio.run(probe_provider(_config(), _ProbeGateway(plan=plan)))

    assert result.status is ProviderProbeStatus.FAILED
    assert result.stage is ProviderProbeStage.PLAN
    assert result.reason_code == "MODEL_TOOL_CONTRACT_INVALID"
    assert result.model_calls == 2


def test_report_is_bound_to_manifest_and_contains_no_secret(tmp_path: Path) -> None:
    configs = [
        _config(PilotFallbackProvider.ELOAGENTS),
        _config(PilotFallbackProvider.GEMINI),
    ]
    report = asyncio.run(
        build_provider_probe_report(
            configs,
            git_commit="a" * 40,
            preflight_manifest_digest="b" * 64,
            gateway_factory=lambda _: _ProbeGateway(),
        )
    )
    output = tmp_path / "provider-probe.json"
    write_provider_probe_report(output, report)

    loaded = load_and_validate_provider_probe(
        output,
        git_commit="a" * 40,
        preflight_manifest_digest="b" * 64,
        expected_providers=[("eloagents", "eloagents-model"), ("gemini", "gemini-model")],
    )
    serialized = output.read_text(encoding="utf-8")

    assert loaded.all_compatible is True
    assert loaded.report_digest == report.report_digest
    assert "must-not-be-serialized" not in serialized
    assert "asset-probe" not in serialized


def test_report_rejects_tampering_and_manifest_drift(tmp_path: Path) -> None:
    config = _config()
    report = asyncio.run(
        build_provider_probe_report(
            [config],
            git_commit="a" * 40,
            preflight_manifest_digest="b" * 64,
            gateway_factory=lambda _: _ProbeGateway(),
        )
    )
    output = tmp_path / "provider-probe.json"
    write_provider_probe_report(output, report)

    with pytest.raises(ValueError, match="PROVIDER_PROBE_STALE"):
        load_and_validate_provider_probe(
            output,
            git_commit="a" * 40,
            preflight_manifest_digest="c" * 64,
            expected_providers=[("gemini", "gemini-model")],
        )

    payload = json.loads(output.read_text(encoding="utf-8"))
    payload["all_compatible"] = False
    output.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="PROVIDER_PROBE_INVALID"):
        load_and_validate_provider_probe(
            output,
            git_commit="a" * 40,
            preflight_manifest_digest="b" * 64,
            expected_providers=[("gemini", "gemini-model")],
        )


def test_failed_report_cannot_authorize_pilot(tmp_path: Path) -> None:
    config = _config(PilotFallbackProvider.ELOAGENTS)
    report = asyncio.run(
        build_provider_probe_report(
            [config],
            git_commit="a" * 40,
            preflight_manifest_digest="b" * 64,
            gateway_factory=lambda _: _ProbeGateway(failure=ModelOutputError("invalid")),
        )
    )
    output = tmp_path / "provider-probe.json"
    write_provider_probe_report(output, report)

    with pytest.raises(ValueError, match="PROVIDER_PROBE_FAILED"):
        load_and_validate_provider_probe(
            output,
            git_commit="a" * 40,
            preflight_manifest_digest="b" * 64,
            expected_providers=[("eloagents", "eloagents-model")],
        )
