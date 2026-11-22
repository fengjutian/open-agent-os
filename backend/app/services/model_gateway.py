import logging
from abc import ABC, abstractmethod

import httpx

from app.core.config import settings
from app.core.errors import AppError, ErrorCode

logger = logging.getLogger(__name__)


class ModelProvider(ABC):
    @abstractmethod
    async def generate(self, *, model: str, messages: list[dict[str, str]]) -> str:
        raise NotImplementedError


class MockProvider(ModelProvider):
    async def generate(self, *, model: str, messages: list[dict[str, str]]) -> str:
        user_text = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        return (
            f"这是 Mock Agent 回复（模型：{model}）。\n\n"
            f"我收到了你的问题：{user_text}\n\n"
            "当前已打通 Agent、会话、消息和模型适配器的基础闭环。"
        )


class OpenAICompatibleProvider(ModelProvider):
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # transport 可注入，便于测试中拦截 HTTP 而不真正联网。
        self._transport = transport

    async def generate(self, *, model: str, messages: list[dict[str, str]]) -> str:
        if not settings.model_api_key:
            raise AppError(
                ErrorCode.MODEL_NOT_CONFIGURED,
                "模型未配置：请设置 MODEL_API_KEY，或将 MODEL_PROVIDER 切回 mock",
                status_code=424,
            )

        try:
            async with httpx.AsyncClient(
                timeout=settings.model_timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    f"{settings.model_base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.model_api_key}"},
                    json={"model": model, "messages": messages, "temperature": 0.2},
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

        try:
            return response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            logger.warning(
                "model provider malformed response",
                extra={"fields": {"model": model}},
            )
            raise AppError(
                ErrorCode.MODEL_PROVIDER_ERROR,
                "模型服务返回了无法解析的响应",
                status_code=502,
            ) from exc


def get_model_provider() -> ModelProvider:
    if settings.model_provider == "openai_compatible":
        return OpenAICompatibleProvider()
    return MockProvider()
