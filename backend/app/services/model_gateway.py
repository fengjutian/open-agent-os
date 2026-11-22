from abc import ABC, abstractmethod
import httpx
from app.core.config import settings

class ModelProvider(ABC):
    @abstractmethod
    async def generate(self, *, model: str, messages: list[dict[str, str]]) -> str:
        raise NotImplementedError

class MockProvider(ModelProvider):
    async def generate(self, *, model: str, messages: list[dict[str, str]]) -> str:
        user_text = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        return f"这是 Mock Agent 回复（模型：{model}）。\n\n我收到了你的问题：{user_text}\n\n当前已打通 Agent、会话、消息和模型适配器的基础闭环。"

class OpenAICompatibleProvider(ModelProvider):
    async def generate(self, *, model: str, messages: list[dict[str, str]]) -> str:
        if not settings.model_api_key:
            raise RuntimeError("MODEL_API_KEY is required")
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                f"{settings.model_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {settings.model_api_key}"},
                json={"model": model, "messages": messages, "temperature": 0.2},
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]

def get_model_provider() -> ModelProvider:
    return OpenAICompatibleProvider() if settings.model_provider == "openai_compatible" else MockProvider()
