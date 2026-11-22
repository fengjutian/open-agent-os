from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Enterprise Agent Platform"
    database_url: str = "sqlite:///./agent_platform.db"
    cors_origins: str = "http://localhost:5173"
    model_provider: str = "mock"
    model_base_url: str = "https://api.openai.com/v1"
    model_api_key: str = ""
    # 创建 Agent 时未显式指定 model 的默认模型（原先该配置从未被使用）。
    model_name: str = "gpt-4o-mini"
    # 单次请求可装入上下文的字符预算，超出则丢弃最旧的历史消息。
    context_max_chars: int = 24000
    # 模型调用超时（秒）。
    model_timeout_seconds: float = 90.0
    # 一次 Agent 运行内允许的最大工具调用轮次，防止无限循环。
    max_tool_iterations: int = 4
    # 认证开关。默认开启；只有本地开发才应该关掉（关掉等于匿名可写）。
    auth_enabled: bool = True
    # 首次启动时用于引导管理员密钥；留空则自动生成并在日志中打印一次。
    bootstrap_admin_api_key: str = ""
    # 每个密钥每分钟允许的请求数；0 表示不限流。
    rate_limit_per_minute: int = 120
    # 每个密钥 24 小时内允许发起的 Agent 运行数；0 表示不限额。
    daily_run_quota: int = 200
    log_level: str = "INFO"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def cors_origin_list(self) -> list[str]:
        return [x.strip() for x in self.cors_origins.split(",") if x.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


settings = Settings()
