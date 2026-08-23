"""Interface experimental de julgamento semântico, fora de qualquer release gate."""

from __future__ import annotations

from typing import Protocol

from indusguard_evals.contracts import JudgeRequest, JudgeVerdict


class JudgeGateway(Protocol):
    """Um juiz recebe uma dimensão por vez para reduzir mistura de critérios."""

    @property
    def model_name(self) -> str: ...

    async def judge(self, request: JudgeRequest) -> JudgeVerdict: ...


class ExternalJudgeNotEnabled(RuntimeError):
    """O envio de tickets e evidências exige uma decisão explícita de privacidade."""


class DisabledExternalJudgeGateway:
    """Default seguro: documenta o seam sem transmitir qualquer dado para terceiros."""

    @property
    def model_name(self) -> str:
        return "disabled"

    async def judge(self, request: JudgeRequest) -> JudgeVerdict:
        del request
        raise ExternalJudgeNotEnabled(
            "LLM-as-a-judge externo não foi habilitado; use revisão humana local."
        )
