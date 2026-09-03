"""Adaptador Gemini reservado ao piloto externo do benchmark."""

from __future__ import annotations

from collections.abc import Mapping
from math import ceil
from typing import Any, Literal
from urllib.parse import urlsplit

import openai
from indusguard_api.agent import (
    AgentConfigurationError,
    ModelRateLimitedError,
    ModelUnavailableError,
)
from indusguard_api.groq_gateway import GroqAgentModelGateway, GroqAgentSettings
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatResult
from langchain_openai import ChatOpenAI
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class GeminiEvalSettings(BaseSettings):
    """Configuração do Gemini usada apenas pelo CLI de avaliação."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_key: SecretStr | None = Field(default=None, validation_alias="GEMINI_API_KEY")
    model: str = Field(
        default="gemini-3.1-flash-lite",
        validation_alias="INDUSGUARD_EVAL_GEMINI_MODEL",
    )
    base_url: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta/openai/",
        validation_alias="INDUSGUARD_EVAL_GEMINI_BASE_URL",
    )
    timeout_seconds: float = Field(
        default=30,
        gt=0,
        le=120,
        validation_alias="INDUSGUARD_EVAL_GEMINI_TIMEOUT_SECONDS",
    )
    max_retries: int = Field(
        default=0,
        ge=0,
        le=2,
        validation_alias="INDUSGUARD_EVAL_GEMINI_MAX_RETRIES",
    )
    max_tokens: int = Field(
        default=2048,
        ge=128,
        le=8192,
        validation_alias="INDUSGUARD_EVAL_GEMINI_MAX_TOKENS",
    )
    reasoning_effort: Literal["minimal", "low"] | None = Field(
        default="minimal",
        validation_alias="INDUSGUARD_EVAL_GEMINI_REASONING_EFFORT",
    )

    @property
    def validated_base_url(self) -> str:
        """Exige URL HTTPS sem credenciais e normaliza o endpoint OpenAI-compatible."""

        normalized = self.base_url.strip()
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
                "INDUSGUARD_EVAL_GEMINI_BASE_URL precisa ser uma URL HTTPS "
                "sem credenciais, query ou fragmento"
            )
        if parsed.netloc == "generativelanguage.googleapis.com":
            path = parsed.path.rstrip("/")
            if path in {"", "/v1beta"}:
                return "https://generativelanguage.googleapis.com/v1beta/openai/"
            if path != "/v1beta/openai":
                raise AgentConfigurationError(
                    "INDUSGUARD_EVAL_GEMINI_BASE_URL precisa apontar para "
                    "https://generativelanguage.googleapis.com/v1beta/openai/"
                )
        return normalized.rstrip("/") + "/"


_TOOL_CALL_CONTINUATIONS_KEY = "indusguard_tool_call_continuations"


def _tool_call_continuations(message: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Extrai assinaturas opacas exigidas pelo Gemini em turnos com tools."""

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


class GeminiCompatibleChatOpenAI(ChatOpenAI):
    """Preserva metadados opacos e remove parâmetros não aceitos pelo endpoint Gemini."""

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
                generation.message,
                AIMessage,
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


def _retry_after_seconds(error: openai.RateLimitError) -> int | None:
    raw = error.response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = ceil(float(raw.strip()))
    except (TypeError, ValueError, OverflowError):
        return None
    return seconds if 0 <= seconds <= 86_400 else None


class GeminiEvalModelGateway(GroqAgentModelGateway):
    """Reutiliza prompts/contratos do agente via endpoint OpenAI-compatible do Gemini."""

    def __init__(
        self,
        settings: GeminiEvalSettings | None = None,
        *,
        chat_factory: Any | None = None,
    ) -> None:
        self._gemini_settings = settings or GeminiEvalSettings()
        if chat_factory is None and (
            self._gemini_settings.api_key is None
            or not self._gemini_settings.api_key.get_secret_value().strip()
        ):
            raise AgentConfigurationError(
                "GEMINI_API_KEY precisa estar definida para criar o adapter Gemini real"
            )
        groq_compatible_settings = GroqAgentSettings(
            GROQ_API_KEY=None,
            INDUSGUARD_GROQ_MODEL=self._gemini_settings.model,
            INDUSGUARD_GROQ_TIMEOUT_SECONDS=min(self._gemini_settings.timeout_seconds, 60),
            INDUSGUARD_GROQ_MAX_RETRIES=self._gemini_settings.max_retries,
            INDUSGUARD_GROQ_MAX_TOKENS=self._gemini_settings.max_tokens,
            _env_file=None,
        )
        super().__init__(
            groq_compatible_settings,
            chat_factory=chat_factory or self._create_chat,
        )

    @property
    def model_name(self) -> str:
        return f"gemini:{self._gemini_settings.model}"

    def _create_chat(self, seed: int) -> BaseChatModel:
        del seed
        api_key = self._gemini_settings.api_key
        if api_key is None:
            raise AgentConfigurationError("GEMINI_API_KEY não está configurada")
        return GeminiCompatibleChatOpenAI(
            model=self._gemini_settings.model,
            api_key=api_key,
            base_url=self._gemini_settings.validated_base_url,
            temperature=None,
            timeout=self._gemini_settings.timeout_seconds,
            max_retries=self._gemini_settings.max_retries,
            max_completion_tokens=self._gemini_settings.max_tokens,
            reasoning_effort=self._gemini_settings.reasoning_effort,
        )

    def _raise_gateway_error(self, exc: Exception) -> None:
        if isinstance(exc, openai.RateLimitError):
            raise ModelRateLimitedError(
                "A cota do Gemini está temporariamente indisponível.",
                retry_after_seconds=_retry_after_seconds(exc),
            ) from exc
        if isinstance(exc, openai.APITimeoutError):
            raise ModelUnavailableError(
                "O Gemini não respondeu dentro do tempo configurado.",
                reason_code="MODEL_TIMEOUT",
            ) from exc
        if isinstance(exc, openai.APIConnectionError):
            raise ModelUnavailableError(
                "Não foi possível estabelecer comunicação com o Gemini.",
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
                "O Gemini rejeitou ou não conseguiu processar a solicitação.",
                reason_code=reason_code,
            ) from exc
        super()._raise_gateway_error(exc)
