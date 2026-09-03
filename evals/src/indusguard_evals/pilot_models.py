"""Fallback de modelos reservado ao piloto externo e ausente do runtime público."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from math import ceil
from typing import Any, Literal, TypeVar, cast
from urllib.parse import urlsplit

import openai
from google.genai import errors as google_errors
from indusguard_api.agent import (
    AgentConfigurationError,
    AgentFinalAnswer,
    AgentIntentDecision,
    AgentModelGateway,
    AgentPlanningContext,
    AgentPlanStep,
    AgentRunRequest,
    AgentRuntimeConfig,
    AgentToolDefinition,
    GatewayResult,
    ModelRateLimitedError,
    ModelUnavailableError,
)
from indusguard_api.groq_gateway import GroqAgentModelGateway, GroqAgentSettings
from indusguard_api.schemas import ConnectorDomain
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

GatewayValue = TypeVar("GatewayValue")


class PilotFallbackProvider(StrEnum):
    ELOAGENTS = "eloagents"
    GEMINI = "gemini"


class OpenAICompatibleProviderConfig(BaseModel):
    """Configuração validada que nunca é serializada diretamente no manifesto."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: PilotFallbackProvider
    base_url: str
    model: str
    api_key: SecretStr
    timeout_seconds: float = Field(gt=0, le=120)
    max_retries: int = Field(ge=0, le=2)
    max_tokens: int = Field(ge=128, le=8192)
    temperature: float | None = Field(default=None, ge=0, le=2)
    reasoning_effort: Literal["minimal", "low"] | None = None


class PilotFallbackSettings(BaseSettings):
    """Opt-in explícito dos fallbacks externos usados somente por ``indusguard-eval``."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    providers: str = Field(
        default="",
        validation_alias="INDUSGUARD_EVAL_FALLBACK_PROVIDERS",
    )
    eloagents_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="ELOAGENTS_API_KEY",
    )
    eloagents_base_url: str = Field(
        default="",
        validation_alias="INDUSGUARD_EVAL_ELOAGENTS_BASE_URL",
    )
    eloagents_model: str = Field(
        default="",
        validation_alias="INDUSGUARD_EVAL_ELOAGENTS_MODEL",
    )
    gemini_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="GEMINI_API_KEY",
    )
    gemini_base_url: str = Field(
        default="https://generativelanguage.googleapis.com/",
        validation_alias="INDUSGUARD_EVAL_GEMINI_BASE_URL",
    )
    gemini_model: str = Field(
        default="",
        validation_alias="INDUSGUARD_EVAL_GEMINI_MODEL",
    )
    gemini_reasoning_effort: Literal["minimal", "low"] = Field(
        default="minimal",
        validation_alias="INDUSGUARD_EVAL_GEMINI_REASONING_EFFORT",
    )
    timeout_seconds: float = Field(
        default=30,
        gt=0,
        le=120,
        validation_alias="INDUSGUARD_EVAL_FALLBACK_TIMEOUT_SECONDS",
    )
    max_retries: int = Field(
        default=0,
        ge=0,
        le=2,
        validation_alias="INDUSGUARD_EVAL_FALLBACK_MAX_RETRIES",
    )
    max_tokens: int = Field(
        default=2048,
        ge=128,
        le=8192,
        validation_alias="INDUSGUARD_EVAL_FALLBACK_MAX_TOKENS",
    )

    def provider_configs(self) -> tuple[OpenAICompatibleProviderConfig, ...]:
        """Resolve a ordem declarada ou falha antes de construir qualquer cliente externo."""

        requested = [item.strip().lower() for item in self.providers.split(",") if item.strip()]
        if len(requested) != len(set(requested)):
            raise AgentConfigurationError("provedores de fallback duplicados")
        try:
            providers = [PilotFallbackProvider(item) for item in requested]
        except ValueError as exc:
            raise AgentConfigurationError(
                "INDUSGUARD_EVAL_FALLBACK_PROVIDERS aceita somente eloagents,gemini"
            ) from exc
        return tuple(self._provider_config(provider) for provider in providers)

    def _provider_config(
        self,
        provider: PilotFallbackProvider,
    ) -> OpenAICompatibleProviderConfig:
        if provider is PilotFallbackProvider.ELOAGENTS:
            api_key = self.eloagents_api_key
            base_url = self.eloagents_base_url
            model = self.eloagents_model
            key_name = "ELOAGENTS_API_KEY"
        else:
            api_key = self.gemini_api_key
            base_url = self.gemini_base_url
            model = self.gemini_model
            key_name = "GEMINI_API_KEY"
        if api_key is None or not api_key.get_secret_value().strip():
            raise AgentConfigurationError(f"{key_name} precisa estar definida")
        if not model.strip():
            raise AgentConfigurationError(
                f"INDUSGUARD_EVAL_{provider.value.upper()}_MODEL precisa estar definido"
            )
        normalized_url = _validated_base_url(base_url, provider)
        return OpenAICompatibleProviderConfig(
            provider=provider,
            base_url=normalized_url,
            model=model.strip(),
            api_key=api_key,
            timeout_seconds=self.timeout_seconds,
            max_retries=self.max_retries,
            max_tokens=self.max_tokens,
            temperature=None if provider is PilotFallbackProvider.GEMINI else 0,
            reasoning_effort=(
                self.gemini_reasoning_effort if provider is PilotFallbackProvider.GEMINI else None
            ),
        )


def _validated_base_url(value: str, provider: PilotFallbackProvider) -> str:
    normalized = value.strip()
    parsed = urlsplit(normalized)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise AgentConfigurationError(
            f"INDUSGUARD_EVAL_{provider.value.upper()}_BASE_URL precisa ser uma URL HTTPS "
            "sem credenciais, query ou fragmento"
        )
    return normalized.rstrip("/") + "/"


def _retry_after_seconds(error: openai.RateLimitError) -> int | None:
    raw = error.response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = ceil(float(raw.strip()))
    except (TypeError, ValueError, OverflowError):
        return None
    return seconds if 0 <= seconds <= 86_400 else None


_TOOL_CALL_CONTINUATIONS_KEY = "indusguard_tool_call_continuations"


def _tool_call_continuations(message: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Extrai somente a assinatura opaca necessária ao próximo turno do Gemini.

    O restante de ``extra_content`` é descartado. A assinatura não é interpretada, logada nem
    persistida; ela volta exclusivamente no tool call com o mesmo ID.
    """

    continuations: dict[str, dict[str, Any]] = {}
    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list):
        return continuations
    for raw_call in raw_calls:
        if not isinstance(raw_call, Mapping) or not isinstance(raw_call.get("id"), str):
            continue
        extra_content = raw_call.get("extra_content")
        google = extra_content.get("google") if isinstance(extra_content, Mapping) else None
        signature = google.get("thought_signature") if isinstance(google, Mapping) else None
        if isinstance(signature, str) and signature:
            continuations[raw_call["id"]] = {"google": {"thought_signature": signature}}
    return continuations


class ContinuationAwareChatOpenAI(ChatOpenAI):
    """Preserva assinaturas de tool calls omitidas pelo conversor genérico do LangChain."""

    def _create_chat_result(
        self,
        response: dict[str, Any] | openai.BaseModel,
        generation_info: dict[str, Any] | None = None,
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)
        response_dict = (
            response if isinstance(response, dict) else response.model_dump(warnings=False)
        )
        choices = response_dict.get("choices", [])
        if not isinstance(choices, list):
            return result
        for generation, choice in zip(result.generations, choices, strict=False):
            raw_message = choice.get("message") if isinstance(choice, Mapping) else None
            if not isinstance(raw_message, Mapping) or not isinstance(
                generation.message, AIMessage
            ):
                continue
            continuations = _tool_call_continuations(raw_message)
            if continuations:
                generation.message.additional_kwargs[_TOOL_CALL_CONTINUATIONS_KEY] = continuations
        return result

    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        # O endpoint Google/Gemini não suporta o parâmetro 'seed' no payload OpenAI-compatible
        if self.openai_api_base and "generativelanguage.googleapis.com" in self.openai_api_base:
            payload.pop("seed", None)
            payload.pop("temperature", None)
        outgoing = payload.get("messages")
        if not isinstance(outgoing, list):
            return payload
        for source, target in zip(messages, outgoing, strict=False):
            if not isinstance(source, AIMessage) or not isinstance(target, dict):
                continue
            continuations = source.additional_kwargs.get(_TOOL_CALL_CONTINUATIONS_KEY)
            target_calls = target.get("tool_calls")
            if not isinstance(continuations, Mapping) or not isinstance(target_calls, list):
                continue
            for target_call in target_calls:
                if not isinstance(target_call, dict):
                    continue
                extra_content = continuations.get(target_call.get("id"))
                if isinstance(extra_content, Mapping):
                    target_call["extra_content"] = dict(extra_content)
        return payload


class OpenAICompatibleAgentModelGateway(GroqAgentModelGateway):
    """Reutiliza os mesmos prompts e contratos através do endpoint compatível."""

    def __init__(self, config: OpenAICompatibleProviderConfig) -> None:
        self._provider_config = config
        settings = GroqAgentSettings(
            GROQ_API_KEY=None,
            INDUSGUARD_GROQ_MODEL=config.model,
            INDUSGUARD_GROQ_TIMEOUT_SECONDS=config.timeout_seconds,
            INDUSGUARD_GROQ_MAX_RETRIES=config.max_retries,
            INDUSGUARD_GROQ_MAX_TOKENS=config.max_tokens,
            _env_file=None,
        )
        super().__init__(settings, chat_factory=self._create_openai_chat)

    def _create_openai_chat(self, seed: int) -> BaseChatModel:
        config = self._provider_config
        return ContinuationAwareChatOpenAI(
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url,
            temperature=config.temperature,
            timeout=config.timeout_seconds,
            max_retries=config.max_retries,
            max_completion_tokens=config.max_tokens,
            seed=seed,
            reasoning_effort=config.reasoning_effort,
        )

    @property
    def model_name(self) -> str:
        """Inclui o provedor para distinguir APIs que exponham o mesmo modelo."""

        return f"{self._provider_config.provider.value}:{self._provider_config.model}"

    def _raise_gateway_error(self, exc: Exception) -> None:
        provider = self._provider_config.provider.value.upper()
        if isinstance(exc, openai.RateLimitError):
            raise ModelRateLimitedError(
                f"{provider} está temporariamente limitado.",
                retry_after_seconds=_retry_after_seconds(exc),
            ) from exc
        if isinstance(exc, openai.APITimeoutError):
            raise ModelUnavailableError(
                f"{provider} não respondeu no tempo configurado.",
                reason_code="MODEL_TIMEOUT",
            ) from exc
        if isinstance(exc, openai.APIConnectionError):
            raise ModelUnavailableError(
                f"Não foi possível estabelecer comunicação com {provider}.",
                reason_code="MODEL_CONNECTION_ERROR",
            ) from exc
        if isinstance(exc, openai.APIStatusError):
            status_code = exc.status_code
            if status_code in {401, 403}:
                reason_code = "MODEL_AUTHENTICATION_ERROR"
            elif status_code == 404:
                reason_code = "MODEL_NOT_FOUND"
            elif 400 <= status_code < 500:
                reason_code = "MODEL_PROVIDER_CLIENT_ERROR"
            else:
                reason_code = "MODEL_PROVIDER_SERVER_ERROR"
            raise ModelUnavailableError(
                f"{provider} rejeitou ou não conseguiu processar a solicitação.",
                reason_code=reason_code,
            ) from exc
        super()._raise_gateway_error(exc)


class NativeGeminiChatGoogleGenerativeAI(ChatGoogleGenerativeAI):
    """Adapta somente a opção não suportada pelo transporte nativo do Google."""

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any]],
        tool_config: dict[str, Any] | None = None,
        *,
        tool_choice: Any = None,
        **kwargs: Any,
    ) -> Any:
        # O runtime já serializa as chamadas; o SDK nativo rejeita esta opção OpenAI.
        kwargs.pop("parallel_tool_calls", None)
        return super().bind_tools(
            tools,
            tool_config=tool_config,
            tool_choice=tool_choice,
            **kwargs,
        )


class GeminiAgentModelGateway(GroqAgentModelGateway):
    """Reutiliza prompts/contratos pelo SDK nativo do Gemini, exclusivo do piloto."""

    def __init__(self, config: OpenAICompatibleProviderConfig) -> None:
        if config.provider is not PilotFallbackProvider.GEMINI:
            raise ValueError("GeminiAgentModelGateway exige configuração Gemini")
        self._provider_config = config
        settings = GroqAgentSettings(
            GROQ_API_KEY=None,
            INDUSGUARD_GROQ_MODEL=config.model,
            INDUSGUARD_GROQ_TIMEOUT_SECONDS=min(config.timeout_seconds, 60),
            INDUSGUARD_GROQ_MAX_RETRIES=config.max_retries,
            INDUSGUARD_GROQ_MAX_TOKENS=config.max_tokens,
            _env_file=None,
        )
        super().__init__(settings, chat_factory=self._create_gemini_chat)

    def _create_gemini_chat(self, seed: int) -> BaseChatModel:
        config = self._provider_config
        return NativeGeminiChatGoogleGenerativeAI(
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url.rstrip("/"),
            api_version="v1beta",
            temperature=config.temperature,
            thinking_level=config.reasoning_effort,
            request_timeout=config.timeout_seconds,
            retries=config.max_retries,
            max_tokens=config.max_tokens,
            seed=seed,
        )

    @property
    def model_name(self) -> str:
        return f"gemini:{self._provider_config.model}"

    def _raise_gateway_error(self, exc: Exception) -> None:
        if isinstance(exc, google_errors.APIError):
            status_code = exc.code
            if status_code == 429:
                raise ModelRateLimitedError("GEMINI está temporariamente limitado.") from exc
            if status_code in {401, 403}:
                reason_code = "MODEL_AUTHENTICATION_ERROR"
            elif status_code == 404:
                reason_code = "MODEL_NOT_FOUND"
            elif status_code in {408, 504}:
                reason_code = "MODEL_TIMEOUT"
            elif 400 <= status_code < 500:
                reason_code = "MODEL_PROVIDER_CLIENT_ERROR"
            else:
                reason_code = "MODEL_PROVIDER_SERVER_ERROR"
            raise ModelUnavailableError(
                "GEMINI rejeitou ou não conseguiu processar a solicitação.",
                reason_code=reason_code,
            ) from exc
        super()._raise_gateway_error(exc)


def build_fallback_gateway(config: OpenAICompatibleProviderConfig) -> AgentModelGateway:
    """Seleciona explicitamente o transporte auditado de cada API externa."""

    if config.provider is PilotFallbackProvider.GEMINI:
        return GeminiAgentModelGateway(config)
    return OpenAICompatibleAgentModelGateway(config)


class WholeRunFallbackGateway:
    """Mantém um provider por run e só avança após reinício explícito da identidade."""

    def __init__(self, gateways: Sequence[AgentModelGateway]) -> None:
        if not gateways:
            raise ValueError("fallback exige ao menos um gateway")
        self._gateways = tuple(gateways)
        self._active_index: int | None = None

    @property
    def model_name(self) -> str:
        if self._active_index is not None:
            return self._gateways[self._active_index].model_name
        models = "->".join(gateway.model_name for gateway in self._gateways)
        return f"whole-run-fallback[{models}]"

    @property
    def runtime_config(self) -> AgentRuntimeConfig:
        """Expõe o orçamento pacing-aware do primário para todas as tentativas."""

        for gateway in self._gateways:
            config = getattr(gateway, "runtime_config", None)
            if isinstance(config, AgentRuntimeConfig):
                return config
        return AgentRuntimeConfig()

    def advance_after_failure(self) -> bool:
        """Seleciona o próximo provider; quem chama deve reiniciar a run inteira."""

        current = self._active_index if self._active_index is not None else 0
        next_index = current + 1
        if next_index >= len(self._gateways):
            return False
        self._active_index = next_index
        return True

    async def _invoke(self, method: str, **kwargs: Any) -> GatewayResult[Any]:
        if self._active_index is None:
            self._active_index = 0
        operation = getattr(self._gateways[self._active_index], method)
        return cast(GatewayResult[Any], await operation(**kwargs))

    async def classify(
        self,
        *,
        request: AgentRunRequest,
        domain: ConnectorDomain,
    ) -> GatewayResult[AgentIntentDecision]:
        return cast(
            GatewayResult[AgentIntentDecision],
            await self._invoke("classify", request=request, domain=domain),
        )

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
        return cast(
            GatewayResult[AgentPlanStep],
            await self._invoke(
                "plan",
                request=request,
                domain=domain,
                intent=intent,
                planning_context=planning_context,
                messages=messages,
                tools=tools,
            ),
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
        return cast(
            GatewayResult[AgentFinalAnswer],
            await self._invoke(
                "finalize",
                request=request,
                domain=domain,
                intent=intent,
                planning_context=planning_context,
                messages=messages,
                allowed_evidence_ids=allowed_evidence_ids,
            ),
        )


def build_pilot_model_gateway(
    primary: AgentModelGateway,
    settings: PilotFallbackSettings,
) -> AgentModelGateway:
    """Monta a cadeia somente quando o opt-in contém fallbacks configurados."""

    fallbacks = [build_fallback_gateway(item) for item in settings.provider_configs()]
    if not fallbacks:
        return primary
    return WholeRunFallbackGateway([primary, *fallbacks])
