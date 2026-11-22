"""Model Gateway：Mock 行为、工具调用解析与错误映射。"""

import asyncio
import json

import httpx
import pytest

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.services.model_gateway import MockProvider, OpenAICompatibleProvider


def run(coro):
    return asyncio.run(coro)


def _provider(handler) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(transport=httpx.MockTransport(handler))


MESSAGES = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "问题"},
]

TOOLS = [
    {
        "type": "function",
        "function": {"name": "echo", "description": "回显", "parameters": {"type": "object"}},
    }
]


def test_mock_provider_echoes_last_user_message():
    reply = run(MockProvider().generate(model="m", messages=MESSAGES))

    assert "问题" in reply.text
    assert "m" in reply.text
    assert reply.tool_calls == ()


def test_openai_compatible_success(monkeypatch):
    monkeypatch.setattr(settings, "model_api_key", "sk-test")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        assert request.headers["Authorization"] == "Bearer sk-test"
        return httpx.Response(200, json={"choices": [{"message": {"content": "答案"}}]})

    reply = run(_provider(handler).generate(model="m", messages=MESSAGES))
    assert reply.text == "答案"
    assert reply.tool_calls == ()


def test_tools_are_sent_in_payload(monkeypatch):
    monkeypatch.setattr(settings, "model_api_key", "sk-test")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    run(_provider(handler).generate(model="m", messages=MESSAGES, tools=TOOLS))

    assert captured["tools"] == TOOLS
    assert captured["tool_choice"] == "auto"


def test_tool_call_is_parsed(monkeypatch):
    monkeypatch.setattr(settings, "model_api_key", "sk-test")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "echo",
                                        "arguments": '{"text": "hi"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    reply = run(_provider(handler).generate(model="m", messages=MESSAGES, tools=TOOLS))

    assert reply.text == ""
    assert len(reply.tool_calls) == 1
    call = reply.tool_calls[0]
    assert (call.id, call.name) == ("call_1", "echo")
    assert call.arguments == {"text": "hi"}


def test_invalid_tool_arguments_json_is_provider_error(monkeypatch):
    monkeypatch.setattr(settings, "model_api_key", "sk-test")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {"name": "echo", "arguments": "{not json"},
                                }
                            ],
                        }
                    }
                ]
            },
        )

    with pytest.raises(AppError) as excinfo:
        run(_provider(handler).generate(model="m", messages=MESSAGES, tools=TOOLS))

    assert excinfo.value.code == ErrorCode.MODEL_PROVIDER_ERROR


def test_empty_reply_is_provider_error(monkeypatch):
    monkeypatch.setattr(settings, "model_api_key", "sk-test")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    with pytest.raises(AppError) as excinfo:
        run(_provider(handler).generate(model="m", messages=MESSAGES))

    assert excinfo.value.code == ErrorCode.MODEL_PROVIDER_ERROR


def test_missing_api_key_maps_to_model_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "model_api_key", "")

    with pytest.raises(AppError) as excinfo:
        run(_provider(lambda request: httpx.Response(200)).generate(model="m", messages=MESSAGES))

    assert excinfo.value.code == ErrorCode.MODEL_NOT_CONFIGURED
    assert excinfo.value.status_code == 424


def test_upstream_error_status_maps_to_provider_error(monkeypatch):
    monkeypatch.setattr(settings, "model_api_key", "sk-test")
    provider = _provider(lambda request: httpx.Response(500, json={"error": "boom"}))

    with pytest.raises(AppError) as excinfo:
        run(provider.generate(model="m", messages=MESSAGES))

    assert excinfo.value.code == ErrorCode.MODEL_PROVIDER_ERROR
    assert excinfo.value.status_code == 502
    assert excinfo.value.details == {"provider_status": 500}
    # 上游响应体不对外泄露
    assert "boom" not in excinfo.value.message


def test_timeout_maps_to_504(monkeypatch):
    monkeypatch.setattr(settings, "model_api_key", "sk-test")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=request)

    with pytest.raises(AppError) as excinfo:
        run(_provider(handler).generate(model="m", messages=MESSAGES))

    assert excinfo.value.code == ErrorCode.MODEL_PROVIDER_ERROR
    assert excinfo.value.status_code == 504


def test_malformed_payload_maps_to_provider_error(monkeypatch):
    monkeypatch.setattr(settings, "model_api_key", "sk-test")
    provider = _provider(lambda request: httpx.Response(200, json={"unexpected": True}))

    with pytest.raises(AppError) as excinfo:
        run(provider.generate(model="m", messages=MESSAGES))

    assert excinfo.value.code == ErrorCode.MODEL_PROVIDER_ERROR
