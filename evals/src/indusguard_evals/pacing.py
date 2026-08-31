"""Decorator de gateway reservado ao controle de vazão do benchmark."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from math import isfinite
from time import monotonic
from typing import TypeVar

from indusguard_api.agent import (
    AgentFinalAnswer,
    AgentIntentDecision,
    AgentModelGateway,
    AgentPlanningContext,
    AgentPlanStep,
    AgentRunRequest,
    AgentRuntimeConfig,
    AgentToolDefinition,
    GatewayResult,
)
from indusguard_api.schemas import ConnectorDomain
from langchain_core.messages import BaseMessage
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

GatewayValue = TypeVar("GatewayValue")


class GroqPilotPacingSettings(BaseSettings):
    """Configuração auditável aplicada somente ao benchmark Groq."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    minimum_interval_seconds: float = Field(
        default=60,
        ge=0,
        le=300,
        validation_alias="INDUSGUARD_EVAL_GROQ_MIN_REQUEST_INTERVAL_SECONDS",
    )


def pacing_aware_runtime_config(
    settings: GroqPilotPacingSettings,
) -> AgentRuntimeConfig:
    """Preserva o orçamento ativo e acrescenta toda espera possível do pacing.

    O gateway é compartilhado entre runs. Portanto, uma run pode aguardar antes de sua
    primeira chamada e entre todas as chamadas seguintes.
    """

    defaults = AgentRuntimeConfig()
    pacing_wait_budget = settings.minimum_interval_seconds * defaults.max_model_calls
    return AgentRuntimeConfig(
        max_model_calls=defaults.max_model_calls,
        max_tool_calls=defaults.max_tool_calls,
        run_timeout_seconds=defaults.run_timeout_seconds + pacing_wait_budget,
        max_evidence_bytes=defaults.max_evidence_bytes,
        max_run_evidence_bytes=defaults.max_run_evidence_bytes,
        prompt_version=defaults.prompt_version,
        policy_version=defaults.policy_version,
    )


class PacedAgentModelGateway:
    """Delega chamadas de modelo pela fronteira experimental de pacing."""

    def __init__(
        self,
        delegate: AgentModelGateway,
        *,
        minimum_interval_seconds: float,
        monotonic: Callable[[], float] = monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not isfinite(minimum_interval_seconds) or minimum_interval_seconds < 0:
            raise ValueError("minimum_interval_seconds precisa ser finito e não negativo")
        self._delegate = delegate
        self._minimum_interval_seconds = minimum_interval_seconds
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last_started_at: float | None = None

    @property
    def model_name(self) -> str:
        return self._delegate.model_name

    @property
    def runtime_config(self) -> AgentRuntimeConfig:
        settings = GroqPilotPacingSettings(
            INDUSGUARD_EVAL_GROQ_MIN_REQUEST_INTERVAL_SECONDS=self._minimum_interval_seconds,
            _env_file=None,
        )
        return pacing_aware_runtime_config(settings)

    async def _paced(
        self,
        operation: Callable[[], Awaitable[GatewayResult[GatewayValue]]],
    ) -> GatewayResult[GatewayValue]:
        async with self._lock:
            current = self._monotonic()
            if self._last_started_at is not None:
                elapsed = current - self._last_started_at
                remaining = self._minimum_interval_seconds - elapsed
                if remaining > 0:
                    await self._sleep(remaining)
            self._last_started_at = self._monotonic()
            return await operation()

    async def classify(
        self,
        *,
        request: AgentRunRequest,
        domain: ConnectorDomain,
    ) -> GatewayResult[AgentIntentDecision]:
        return await self._paced(lambda: self._delegate.classify(request=request, domain=domain))

    async def plan(
        self,
        *,
        request: AgentRunRequest,
        domain: ConnectorDomain,
        intent: AgentIntentDecision,
        planning_context: AgentPlanningContext,
        messages: Sequence[BaseMessage],
        tools: Sequence[AgentToolDefinition],
    ) -> GatewayResult[AgentPlanStep]:
        return await self._paced(
            lambda: self._delegate.plan(
                request=request,
                domain=domain,
                intent=intent,
                planning_context=planning_context,
                messages=messages,
                tools=tools,
            )
        )

    async def finalize(
        self,
        *,
        request: AgentRunRequest,
        domain: ConnectorDomain,
        intent: AgentIntentDecision,
        planning_context: AgentPlanningContext,
        messages: Sequence[BaseMessage],
        allowed_evidence_ids: Sequence[str],
    ) -> GatewayResult[AgentFinalAnswer]:
        return await self._paced(
            lambda: self._delegate.finalize(
                request=request,
                domain=domain,
                intent=intent,
                planning_context=planning_context,
                messages=messages,
                allowed_evidence_ids=allowed_evidence_ids,
            )
        )
