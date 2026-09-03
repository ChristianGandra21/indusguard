"""Fallback do piloto troca provider somente depois de encerrar a run atual."""

import asyncio
from collections import deque
from typing import Any, cast

import httpx
import openai
import pytest
from indusguard_api.agent import (
    AgentConfigurationError,
    AgentIntentDecision,
    AgentModelGateway,
    GatewayResult,
    ModelOutputError,
    ModelRateLimitedError,
    ModelUnavailableError,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import SecretStr

from indusguard_evals.pilot_models import (
    ContinuationAwareChatOpenAI,
    OpenAICompatibleAgentModelGateway,
    OpenAICompatibleProviderConfig,
    PilotFallbackProvider,
    PilotFallbackSettings,
    WholeRunFallbackGateway,
    build_pilot_model_gateway,
)


class _Gateway:
    def __init__(self, name: str, outcomes: list[object]) -> None:
        self._name = name
        self._outcomes = deque(outcomes)
        self.calls = 0

    @property
    def model_name(self) -> str:
        return self._name

    async def classify(self, **_: Any) -> GatewayResult[AgentIntentDecision]:
        self.calls += 1
        outcome = self._outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return cast(GatewayResult[AgentIntentDecision], outcome)


def _result(intent: str) -> GatewayResult[AgentIntentDecision]:
    return GatewayResult(AgentIntentDecision(intent_id=intent))


def _classify(gateway: WholeRunFallbackGateway) -> GatewayResult[AgentIntentDecision]:
    return asyncio.run(gateway.classify(request=cast(Any, object()), domain=cast(Any, object())))


def test_settings_require_complete_opt_in_without_exposing_keys() -> None:
    settings = PilotFallbackSettings(
        INDUSGUARD_EVAL_FALLBACK_PROVIDERS="eloagents,gemini",
        ELOAGENTS_API_KEY="elo-secret",
        INDUSGUARD_EVAL_ELOAGENTS_BASE_URL="https://elo.example/v1",
        INDUSGUARD_EVAL_ELOAGENTS_MODEL="gemini-pro-from-elo",
        GEMINI_API_KEY="gemini-secret",
        INDUSGUARD_EVAL_GEMINI_MODEL="gemini-direct",
        _env_file=None,
    )

    providers = settings.provider_configs()

    assert [item.provider for item in providers] == [
        PilotFallbackProvider.ELOAGENTS,
        PilotFallbackProvider.GEMINI,
    ]
    assert providers[0].base_url == "https://elo.example/v1/"
    serialized = repr(providers)
    assert "elo-secret" not in serialized
    assert "gemini-secret" not in serialized


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"INDUSGUARD_EVAL_FALLBACK_PROVIDERS": "unknown"}, "aceita somente"),
        ({"INDUSGUARD_EVAL_FALLBACK_PROVIDERS": "eloagents"}, "ELOAGENTS_API_KEY"),
        (
            {
                "INDUSGUARD_EVAL_FALLBACK_PROVIDERS": "eloagents",
                "ELOAGENTS_API_KEY": "secret",
                "INDUSGUARD_EVAL_ELOAGENTS_MODEL": "model",
                "INDUSGUARD_EVAL_ELOAGENTS_BASE_URL": "http://elo.example/v1",
            },
            "URL HTTPS",
        ),
    ],
)
def test_settings_reject_unknown_incomplete_or_insecure_providers(
    values: dict[str, str],
    message: str,
) -> None:
    settings = PilotFallbackSettings(**values, _env_file=None)

    with pytest.raises(AgentConfigurationError, match=message):
        settings.provider_configs()


def test_first_availability_failure_uses_fallback_and_then_sticks() -> None:
    primary = _Gateway("groq", [ModelRateLimitedError("limited")])
    fallback = _Gateway("eloagents", [_result("investigar"), _result("investigar")])
    gateway = WholeRunFallbackGateway(
        [cast(AgentModelGateway, primary), cast(AgentModelGateway, fallback)]
    )

    with pytest.raises(ModelRateLimitedError):
        _classify(gateway)

    assert gateway.advance_after_failure() is True
    assert gateway.model_name == "eloagents"
    assert _classify(gateway).value.intent_id == "investigar"
    assert _classify(gateway).value.intent_id == "investigar"
    assert primary.calls == 1
    assert fallback.calls == 2


def test_output_error_is_scored_without_falling_back() -> None:
    primary = _Gateway("groq", [ModelOutputError("invalid"), _result("investigar")])
    fallback = _Gateway("eloagents", [_result("other")])
    gateway = WholeRunFallbackGateway(
        [cast(AgentModelGateway, primary), cast(AgentModelGateway, fallback)]
    )

    with pytest.raises(ModelOutputError, match="invalid"):
        _classify(gateway)

    assert gateway.model_name == "groq"
    assert _classify(gateway).value.intent_id == "investigar"
    assert fallback.calls == 0


def test_selected_provider_failure_does_not_mix_models() -> None:
    primary = _Gateway(
        "groq",
        [_result("investigar"), ModelUnavailableError("down")],
    )
    fallback = _Gateway("eloagents", [_result("other")])
    gateway = WholeRunFallbackGateway(
        [cast(AgentModelGateway, primary), cast(AgentModelGateway, fallback)]
    )

    assert _classify(gateway).value.intent_id == "investigar"
    with pytest.raises(ModelUnavailableError, match="down"):
        _classify(gateway)

    assert fallback.calls == 0
    assert gateway.advance_after_failure() is True
    assert _classify(gateway).value.intent_id == "other"


def test_empty_fallback_configuration_preserves_primary_gateway() -> None:
    primary = cast(AgentModelGateway, _Gateway("groq", [_result("investigar")]))

    gateway = build_pilot_model_gateway(
        primary,
        PilotFallbackSettings(_env_file=None),
    )

    assert gateway is primary


def test_openai_compatible_rate_limit_is_redacted_and_retains_retry_after() -> None:
    config = OpenAICompatibleProviderConfig(
        provider=PilotFallbackProvider.GEMINI,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        model="gemini-test",
        api_key=SecretStr("never-print-this"),
        timeout_seconds=30,
        max_retries=0,
        max_tokens=2048,
        temperature=1,
    )
    gateway = OpenAICompatibleAgentModelGateway(config)
    chat = gateway._create_openai_chat(42)
    request = httpx.Request("POST", "https://provider.invalid/chat/completions")
    response = httpx.Response(429, request=request, headers={"retry-after": "12.2"})
    error = openai.RateLimitError("provider body must stay private", response=response, body=None)

    with pytest.raises(ModelRateLimitedError) as captured:
        gateway._raise_gateway_error(error)

    assert captured.value.retry_after_seconds == 13
    assert "provider body" not in str(captured.value)
    assert "never-print-this" not in str(captured.value)
    assert gateway.model_name == "gemini:gemini-test"
    assert chat.seed == 42
    assert chat.max_tokens == 2048
    assert chat.temperature == 1


def test_openai_compatible_chat_round_trips_gemini_tool_call_signature() -> None:
    chat = ContinuationAwareChatOpenAI(
        model="gemini-3.7-flash",
        api_key=SecretStr("test-key"),
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        temperature=1,
        max_retries=0,
    )
    raw_response = {
        "id": "response-1",
        "model": "gemini-3.7-flash",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "get_asset", "arguments": '{"id":"a-1"}'},
                            "extra_content": {"google": {"thought_signature": "opaque-signature"}},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }

    result = chat._create_chat_result(raw_response)
    provider_message = result.generations[0].message
    assert isinstance(provider_message, AIMessage)

    payload = chat._get_request_payload(
        [
            HumanMessage(content="Consulte o ativo."),
            provider_message,
            ToolMessage(content='{"status":"ok"}', tool_call_id="call-1"),
        ]
    )

    assert payload["messages"][1]["tool_calls"][0]["extra_content"] == {
        "google": {"thought_signature": "opaque-signature"}
    }
    assert "opaque-signature" not in repr(provider_message.tool_calls)
