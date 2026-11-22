"""模型网关。

Provider 只负责「输入消息 → 输出回复」，其中回复可能包含工具调用请求。
注意：模型**只能提出**工具调用请求，是否执行由 app.services.tool_gateway 决定。
"""

import json
import logging
from abc import ABC, abstractmethod
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


class ModelProvider(ABC):
    @abstractmethod
    async def generate(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelReply:
        raise NotImplementedError


class MockProvider(ModelProvider):
    """确定性 Mock：只产出文本，不发起工具调用。"""

    async def generate(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelReply:
        user_text = next(
            (m.get("content") or "" for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        text = (
            f"这是 Mock Agent 回复（模型：{model}）。\n\n"
            f"我收到了你的问题：{user_text}\n\n"
            "当前已打通 Agent、会话、消息和模型适配器的基础闭环。"
        )
        return ModelReply(text=text)


class OpenAICompatibleProvider(ModelProvider):
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # transport 可注入，便于测试中拦截 HTTP 而不真正联网。
        self._transport = transport

    async def generate(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelReply:
        if not settings.model_api_key:
            raise AppError(
                ErrorCode.MODEL_NOT_CONFIGURED,
                "模型未配置：请设置 MODEL_API_KEY，或将 MODEL_PROVIDER 切回 mock",
                status_code=424,
            )

        payload: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0.2}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        try:
            async with httpx.AsyncClient(
                timeout=settings.model_timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    f"{settings.model_base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.model_api_key}"},
                    json=payload,
                )
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

        if response.status_code >= 400:
            # 上游响应体可能含敏感信息，只记状态码，不对外回显。
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

        return self._parse(response)

    @staticmethod
    def _parse(response: httpx.Response) -> ModelReply:
        try:
            message = response.json()["choices"][0]["message"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            logger.warning("model provider malformed response")
            raise AppError(
                ErrorCode.MODEL_PROVIDER_ERROR,
                "模型服务返回了无法解析的响应",
                status_code=502,
            ) from exc

        text = message.get("content") or ""
        raw_calls = message.get("tool_calls") or []

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
            tool_calls.append(
                ToolCall(id=raw.get("id") or f"call_{index}", name=name, arguments=arguments)
            )

        if not text and not tool_calls:
            raise AppError(
                ErrorCode.MODEL_PROVIDER_ERROR,
                "模型返回了空回复",
                status_code=502,
            )

        return ModelReply(text=text, tool_calls=tuple(tool_calls))


def get_model_provider() -> ModelProvider:
    if settings.model_provider == "openai_compatible":
        return OpenAICompatibleProvider()
    return MockProvider()
