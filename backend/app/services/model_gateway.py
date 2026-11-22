"""模型网关。

Provider 只负责「输入消息 → 输出回复」，其中回复可能包含工具调用请求。
注意：模型**只能提出**工具调用请求，是否执行由 app.services.tool_gateway 决定。

流式设计：`stream()` 是唯一实现入口，`generate()` 只是把它收集成完整回复。
这样非流式与流式共享同一套解析逻辑，不会出现两套行为不一致。
"""

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import AppError, ErrorCode

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolCall:
    """模型提出的一个工具调用请求。"""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelReply:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TextDelta:
    """增量文本。"""

    text: str


@dataclass(frozen=True)
class StreamDone:
    """流结束，携带完整回复。"""

    reply: ModelReply


StreamEvent = TextDelta | StreamDone


def _tool_calls_from_raw(raw_calls: list[dict[str, Any]]) -> tuple[ToolCall, ...]:
    tool_calls: list[ToolCall] = []
    for index, raw in enumerate(raw_calls):
        function = raw.get("function") or {}
        name = function.get("name") or ""
        raw_arguments = function.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        except json.JSONDecodeError as exc:
            raise AppError(
                ErrorCode.MODEL_PROVIDER_ERROR,
                "模型返回的工具参数不是合法 JSON",
                status_code=502,
            ) from exc
        if not isinstance(arguments, dict) or not name:
            raise AppError(
                ErrorCode.MODEL_PROVIDER_ERROR,
                "模型返回了无法解析的工具调用",
                status_code=502,
            )
        tool_calls.append(ToolCall(id=raw.get("id") or f"call_{index}", name=name, arguments=arguments))
    return tuple(tool_calls)


class ModelProvider(ABC):
    @abstractmethod
    def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """产出增量文本，最后产出一个 StreamDone。"""
        raise NotImplementedError

    async def generate(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelReply:
        """把流式结果收集成完整回复（非流式调用方使用）。"""
        reply = ModelReply()
        async for event in self.stream(model=model, messages=messages, tools=tools):
            if isinstance(event, StreamDone):
                reply = event.reply
        return reply


class MockProvider(ModelProvider):
    """确定性 Mock：只产出文本，不发起工具调用。"""

    CHUNK_SIZE = 8

    def _render(self, model: str, messages: list[dict[str, Any]]) -> str:
        user_text = next(
            (m.get("content") or "" for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        return (
            f"这是 Mock Agent 回复（模型：{model}）。\n\n"
            f"我收到了你的问题：{user_text}\n\n"
            "当前已打通 Agent、会话、消息和模型适配器的基础闭环。"
        )

    async def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        text = self._render(model, messages)
        for start in range(0, len(text), self.CHUNK_SIZE):
            yield TextDelta(text=text[start : start + self.CHUNK_SIZE])
        yield StreamDone(reply=ModelReply(text=text))


class OpenAICompatibleProvider(ModelProvider):
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # transport 可注入，便于测试中拦截 HTTP 而不真正联网。
        self._transport = transport

    async def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        if not settings.model_api_key:
            raise AppError(
                ErrorCode.MODEL_NOT_CONFIGURED,
                "模型未配置：请设置 MODEL_API_KEY，或将 MODEL_PROVIDER 切回 mock",
                status_code=424,
            )

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        try:
            async with (
                httpx.AsyncClient(
                    timeout=settings.model_timeout_seconds,
                    transport=self._transport,
                ) as client,
                client.stream(
                    "POST",
                    f"{settings.model_base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.model_api_key}"},
                    json=payload,
                ) as response,
            ):
                if response.status_code >= 400:
                    await response.aread()
                    logger.warning(
                        "model provider bad status",
                        extra={"fields": {"model": model, "provider_status": response.status_code}},
                    )
                    raise AppError(
                        ErrorCode.MODEL_PROVIDER_ERROR,
                        f"模型服务返回错误状态 {response.status_code}",
                        status_code=502,
                        details={"provider_status": response.status_code},
                    )

                content_type = response.headers.get("content-type", "")
                if "text/event-stream" not in content_type:
                    # 有些网关会忽略 stream=true，直接返回整包 JSON：兼容处理。
                    body = await response.aread()
                    yield StreamDone(reply=self._parse_whole_body(body))
                    return

                async for event in self._consume_sse(response):
                    yield event
        except httpx.TimeoutException as exc:
            logger.warning("model provider timeout", extra={"fields": {"model": model}})
            raise AppError(
                ErrorCode.MODEL_PROVIDER_ERROR,
                "模型服务响应超时，请稍后重试",
                status_code=504,
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning(
                "model provider transport error",
                extra={"fields": {"model": model, "error": type(exc).__name__}},
            )
            raise AppError(
                ErrorCode.MODEL_PROVIDER_ERROR,
                "模型服务不可用，请稍后重试",
                status_code=502,
            ) from exc

    async def _consume_sse(self, response: httpx.Response) -> AsyncIterator[StreamEvent]:
        text_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}

        async for line in response.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                logger.warning("model provider sent malformed SSE chunk")
                continue

            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}

            content = delta.get("content")
            if content:
                text_parts.append(content)
                yield TextDelta(text=content)

            for item in delta.get("tool_calls") or []:
                index = int(item.get("index", 0))
                slot = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                if item.get("id"):
                    slot["id"] = item["id"]
                function = item.get("function") or {}
                # name 通常一次到齐，arguments 是分片累积。
                if function.get("name"):
                    slot["name"] = function["name"]
                if function.get("arguments"):
                    slot["arguments"] += function["arguments"]

        text = "".join(text_parts)
        tool_calls: tuple[ToolCall, ...] = ()
        if calls:
            raw_calls = [
                {
                    "id": calls[index]["id"],
                    "function": {
                        "name": calls[index]["name"],
                        "arguments": calls[index]["arguments"] or "{}",
                    },
                }
                for index in sorted(calls)
            ]
            tool_calls = _tool_calls_from_raw(raw_calls)

        if not text and not tool_calls:
            raise AppError(
                ErrorCode.MODEL_PROVIDER_ERROR,
                "模型返回了空回复",
                status_code=502,
            )
        yield StreamDone(reply=ModelReply(text=text, tool_calls=tool_calls))

    @staticmethod
    def _parse_whole_body(body: bytes) -> ModelReply:
        try:
            payload = json.loads(body)
            message = payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            logger.warning("model provider malformed response")
            raise AppError(
                ErrorCode.MODEL_PROVIDER_ERROR,
                "模型服务返回了无法解析的响应",
                status_code=502,
            ) from exc

        text = message.get("content") or ""
        tool_calls = _tool_calls_from_raw(message.get("tool_calls") or [])
        if not text and not tool_calls:
            raise AppError(
                ErrorCode.MODEL_PROVIDER_ERROR,
                "模型返回了空回复",
                status_code=502,
            )
        return ModelReply(text=text, tool_calls=tool_calls)


def get_model_provider() -> ModelProvider:
    if settings.model_provider == "openai_compatible":
        return OpenAICompatibleProvider()
    return MockProvider()
