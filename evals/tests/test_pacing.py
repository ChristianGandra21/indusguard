"""Pacing determinístico evita recriar o mesmo 429 dentro de uma run."""

import asyncio

import pytest
from indusguard_api.agent import (
    AgentIntentDecision,
    AgentRunRequest,
    GatewayResult,
    ModelRateLimitedError,
)

from indusguard_evals.pacing import PacedAgentModelGateway


class VirtualClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class WindowLimitedGateway:
    """Simula o provedor recusando duas chamadas na mesma janela de TPM."""

    def __init__(self, clock: VirtualClock, *, window_seconds: float = 60.0) -> None:
        self._clock = clock
        self._window_seconds = window_seconds
        self._last_started_at: float | None = None
        self.active_calls = 0
        self.max_active_calls = 0

    @property
    def model_name(self) -> str:
        return "window-limited-test-model"

    async def classify(self, **_: object) -> GatewayResult[AgentIntentDecision]:
        started_at = self._clock.monotonic()
        if (
            self._last_started_at is not None
            and started_at - self._last_started_at < self._window_seconds
        ):
            raise ModelRateLimitedError("limite simulado")
        self._last_started_at = started_at
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        await asyncio.sleep(0)
        self.active_calls -= 1
        return GatewayResult(AgentIntentDecision(intent_id="consultar"))


REQUEST = AgentRunRequest(connector_id="synthetic", message="Consulte o widget.")


def test_pacing_prevents_the_intrarun_rate_limit_reproduction() -> None:
    async def exercise() -> tuple[list[float], int]:
        unpaced_clock = VirtualClock()
        unpaced = WindowLimitedGateway(unpaced_clock)
        await unpaced.classify(request=REQUEST, domain=object())
        with pytest.raises(ModelRateLimitedError):
            await unpaced.classify(request=REQUEST, domain=object())

        clock = VirtualClock()
        delegate = WindowLimitedGateway(clock)
        gateway = PacedAgentModelGateway(
            delegate,
            minimum_interval_seconds=60,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        await gateway.classify(request=REQUEST, domain=object())
        clock.now += 15
        await gateway.classify(request=REQUEST, domain=object())
        return clock.sleeps, delegate.max_active_calls

    sleeps, max_active_calls = asyncio.run(exercise())

    assert sleeps == [45]
    assert max_active_calls == 1


def test_pacing_serializes_concurrent_model_calls() -> None:
    async def exercise() -> tuple[list[float], int]:
        clock = VirtualClock()
        delegate = WindowLimitedGateway(clock)
        gateway = PacedAgentModelGateway(
            delegate,
            minimum_interval_seconds=60,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        await asyncio.gather(
            gateway.classify(request=REQUEST, domain=object()),
            gateway.classify(request=REQUEST, domain=object()),
        )
        return clock.sleeps, delegate.max_active_calls

    sleeps, max_active_calls = asyncio.run(exercise())

    assert sleeps == [60]
    assert max_active_calls == 1


def test_zero_interval_keeps_calls_unpaced_but_serialized() -> None:
    async def exercise() -> tuple[list[float], int]:
        clock = VirtualClock()
        delegate = WindowLimitedGateway(clock, window_seconds=0)
        gateway = PacedAgentModelGateway(
            delegate,
            minimum_interval_seconds=0,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        await asyncio.gather(
            gateway.classify(request=REQUEST, domain=object()),
            gateway.classify(request=REQUEST, domain=object()),
        )
        return clock.sleeps, delegate.max_active_calls

    sleeps, max_active_calls = asyncio.run(exercise())

    assert sleeps == []
    assert max_active_calls == 1
