"""Fallback externo reinicia a identidade completa e preserva uma trilha redigida."""

import asyncio
from collections import deque
from typing import Any, cast

from indusguard_api.agent import AgentModelGateway, AgentTerminationReason

from indusguard_evals.contracts import (
    EvaluationSample,
    EvaluationVariant,
    ScheduledRun,
)
from indusguard_evals.execution import FallbackVariantRuntime
from indusguard_evals.pilot_models import WholeRunFallbackGateway
from tests.factories import agent_result


class _Gateway:
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name


class _VariantRuntime:
    variant = EvaluationVariant.GUARDED

    def __init__(self, samples: list[EvaluationSample]) -> None:
        self._samples = deque(samples)
        self.calls = 0

    async def run(self, *_: Any) -> EvaluationSample:
        self.calls += 1
        return self._samples.popleft()


def _sample(
    termination: AgentTerminationReason,
    *,
    model: str,
    run_id: str,
    retry_after_seconds: int | None = None,
) -> EvaluationSample:
    result = agent_result(termination=termination)
    metrics = result.metrics.model_copy(
        update={
            "model": model,
            "retry_after_seconds": retry_after_seconds,
        }
    )
    return EvaluationSample(
        scheduled=ScheduledRun(
            case_id="case_tkt_inv_04",
            scenario_id="CEN-01",
            variant=EvaluationVariant.GUARDED,
            seed=11,
            ordinal=0,
        ),
        result=result.model_copy(update={"run_id": run_id, "metrics": metrics}),
    )


def test_rate_limit_restarts_whole_identity_on_next_provider() -> None:
    failed = _sample(
        AgentTerminationReason.MODEL_RATE_LIMITED,
        model="openai/gpt-oss-20b",
        run_id="00000000-0000-0000-0000-000000000001",
        retry_after_seconds=900,
    )
    completed = _sample(
        AgentTerminationReason.COMPLETED,
        model="gemini:gemini-3.7-flash",
        run_id="00000000-0000-0000-0000-000000000002",
    )
    delegate = _VariantRuntime([failed, completed])
    controller = WholeRunFallbackGateway(
        [
            cast(AgentModelGateway, _Gateway("openai/gpt-oss-20b")),
            cast(AgentModelGateway, _Gateway("gemini:gemini-3.7-flash")),
        ]
    )
    runtime = FallbackVariantRuntime(cast(Any, delegate), controller)

    sample = asyncio.run(runtime.run(failed.scheduled, cast(Any, object())))

    assert sample.result.metrics.termination_reason is AgentTerminationReason.COMPLETED
    assert delegate.calls == 2
    assert [item.model for item in sample.model_provider_attempts] == [
        "openai/gpt-oss-20b",
        "gemini:gemini-3.7-flash",
    ]
    assert [item.termination_reason for item in sample.model_provider_attempts] == [
        "MODEL_RATE_LIMITED",
        "COMPLETED",
    ]


def test_model_output_error_is_not_retried_on_another_provider() -> None:
    failed = _sample(
        AgentTerminationReason.MODEL_OUTPUT_INVALID,
        model="openai/gpt-oss-20b",
        run_id="00000000-0000-0000-0000-000000000003",
    )
    delegate = _VariantRuntime([failed])
    controller = WholeRunFallbackGateway(
        [
            cast(AgentModelGateway, _Gateway("openai/gpt-oss-20b")),
            cast(AgentModelGateway, _Gateway("gemini:gemini-3.7-flash")),
        ]
    )
    runtime = FallbackVariantRuntime(cast(Any, delegate), controller)

    sample = asyncio.run(runtime.run(failed.scheduled, cast(Any, object())))

    assert sample.result.metrics.termination_reason is AgentTerminationReason.MODEL_OUTPUT_INVALID
    assert delegate.calls == 1
    assert controller.model_name == (
        "whole-run-fallback[openai/gpt-oss-20b->gemini:gemini-3.7-flash]"
    )


def test_all_rate_limits_preserve_longest_retry_window() -> None:
    primary = _sample(
        AgentTerminationReason.MODEL_RATE_LIMITED,
        model="openai/gpt-oss-20b",
        run_id="00000000-0000-0000-0000-000000000004",
        retry_after_seconds=1200,
    )
    fallback = _sample(
        AgentTerminationReason.MODEL_RATE_LIMITED,
        model="gemini:gemini-3.7-flash",
        run_id="00000000-0000-0000-0000-000000000005",
        retry_after_seconds=30,
    )
    delegate = _VariantRuntime([primary, fallback])
    controller = WholeRunFallbackGateway(
        [
            cast(AgentModelGateway, _Gateway("openai/gpt-oss-20b")),
            cast(AgentModelGateway, _Gateway("gemini:gemini-3.7-flash")),
        ]
    )
    runtime = FallbackVariantRuntime(cast(Any, delegate), controller)

    sample = asyncio.run(runtime.run(primary.scheduled, cast(Any, object())))

    assert sample.result.metrics.termination_reason is AgentTerminationReason.MODEL_RATE_LIMITED
    assert sample.result.metrics.retry_after_seconds == 1200
    assert len(sample.model_provider_attempts) == 2
