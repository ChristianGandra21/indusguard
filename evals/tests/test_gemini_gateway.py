"""Gemini fica isolado no pacote evals e redige erros do provider."""

import httpx
import openai
import pytest
from indusguard_api.agent import (
    AgentConfigurationError,
    ModelRateLimitedError,
    ModelUnavailableError,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from indusguard_evals.gemini_gateway import (
    GeminiCompatibleChatOpenAI,
    GeminiEvalModelGateway,
    GeminiEvalSettings,
)


def test_gemini_settings_require_key_and_https_base_url() -> None:
    with pytest.raises(AgentConfigurationError, match="GEMINI_API_KEY"):
        GeminiEvalModelGateway(GeminiEvalSettings(GEMINI_API_KEY=None, _env_file=None))

    settings = GeminiEvalSettings(
        GEMINI_API_KEY="secret",
        INDUSGUARD_EVAL_GEMINI_BASE_URL="http://provider.test/openai",
        _env_file=None,
    )

    with pytest.raises(AgentConfigurationError, match="URL HTTPS"):
        _ = settings.validated_base_url

    root_settings = GeminiEvalSettings(
        GEMINI_API_KEY="secret",
        INDUSGUARD_EVAL_GEMINI_BASE_URL="https://generativelanguage.googleapis.com/",
        _env_file=None,
    )
    assert (
        root_settings.validated_base_url
        == "https://generativelanguage.googleapis.com/v1beta/openai/"
    )

    with pytest.raises(AgentConfigurationError, match="v1beta/openai"):
        _ = GeminiEvalSettings(
            GEMINI_API_KEY="secret",
            INDUSGUARD_EVAL_GEMINI_BASE_URL="https://generativelanguage.googleapis.com/v1beta/models",
            _env_file=None,
        ).validated_base_url


def test_gemini_chat_strips_unsupported_parameters_and_preserves_tool_signature() -> None:
    chat = GeminiCompatibleChatOpenAI(
        model="gemini-test",
        api_key="test-key",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        temperature=1,
        max_retries=0,
        seed=42,
    )
    raw_response = {
        "id": "response-1",
        "model": "gemini-test",
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
                            "extra_content": {"google": {"thought_signature": "opaque"}},
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
        "google": {"thought_signature": "opaque"}
    }
    assert "temperature" not in payload
    assert "seed" not in payload
    assert "opaque" not in repr(provider_message.tool_calls)


def test_gemini_errors_are_redacted_and_classified() -> None:
    gateway = GeminiEvalModelGateway(
        GeminiEvalSettings(GEMINI_API_KEY="never-print-this", _env_file=None)
    )
    request = httpx.Request("POST", "https://provider.invalid/chat/completions")
    response = httpx.Response(429, request=request, headers={"retry-after": "12.2"})
    error = openai.RateLimitError("provider body must stay private", response=response, body=None)

    with pytest.raises(ModelRateLimitedError) as captured:
        gateway._raise_gateway_error(error)

    assert captured.value.retry_after_seconds == 13
    assert "provider body" not in str(captured.value)
    assert "never-print-this" not in str(captured.value)

    unavailable = openai.APIStatusError(
        "provider body must stay private",
        response=httpx.Response(401, request=request),
        body=None,
    )
    with pytest.raises(ModelUnavailableError) as unavailable_capture:
        gateway._raise_gateway_error(unavailable)
    assert unavailable_capture.value.reason_code == "MODEL_AUTHENTICATION_ERROR"
    assert "provider body" not in str(unavailable_capture.value)
