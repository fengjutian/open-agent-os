from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class McpServerConfig(BaseModel):
    """一个 MCP stdio 服务器的启动方式。

    注意：MCP 服务器**只能**在这里配置（环境变量），不能通过 API 注册。
    否则拥有 write:tools 的管理员就能借「注册工具」执行任意二进制。
    数据库里的 ToolDefinition 只通过 `config.server` 引用这里的名字。
    """

    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = 10.0


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
    # 初始管理员密钥。留空则首次启动自动生成并在日志中打印一次。
    bootstrap_admin_api_key: str = ""
    # ---- OIDC / JWT（可选，与 API Key 并存）----
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""
    # 从哪个 claim 取角色；取不到或非法时回落到 oidc_default_role
    oidc_role_claim: str = "role"
    oidc_default_role: str = "viewer"
    # 从哪个 claim 取租户；取不到时落到默认租户
    oidc_tenant_claim: str = "tenant"
    # true 时要求 OIDC 用户必须已加入该租户（没有 membership 直接 403）
    oidc_require_membership: bool = False
    oidc_algorithms: str = "RS256"
    oidc_jwks_cache_seconds: int = 300
    # 每个密钥每分钟允许的请求数；0 表示不限流。
    rate_limit_per_minute: int = 120
    # 限流后端：memory（进程内）或 redis（多副本共享）
    rate_limit_backend: str = "memory"
    redis_url: str = ""
    # 每个密钥 24 小时内允许发起的 Agent 运行数；0 表示不限额。
    daily_run_quota: int = 200
    # MCP 服务器清单，形如 MCP_SERVERS='{"echo": {"command": "python", "args": ["-m", "srv"]}}'
    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict)
    # 单次 MCP 调用允许的最长等待（秒），与工具自身的 timeout 取较小值。
    mcp_timeout_seconds: float = 30.0
    # MCP 连接空闲多久后回收（秒）；0 表示每次调用都重连
    mcp_idle_seconds: float = 60.0
    # 历史装不下时用摘要替代"整条丢弃"；只在超过预算的 trigger_ratio 时触发
    context_summary_enabled: bool = True
    context_summary_trigger_ratio: float = 0.8
    context_summary_keep_messages: int = 6
    context_summary_max_chars: int = 2000
    # 会话模式默认值：ask / plan / craft（见 services/conversation_modes.py）
    default_conversation_mode: str = "craft"
    # 定时任务：是否启动后台调度循环，以及轮询间隔（秒）
    cron_enabled: bool = True
    cron_tick_seconds: float = 30.0
    # 单次调度最多补跑多少个错过的任务（防止停机很久后一次性刷屏）
    cron_max_due_per_tick: int = 20
    # 回放已落库事件时的轮询间隔（秒）
    run_stream_poll_seconds: float = 0.2
    # 回放端点最长跟随多久（秒）；超时后以 stream.end(reason=timeout) 收尾
    run_stream_max_seconds: float = 120.0
    # 注入 system prompt 的技能内容字符上限，超出则截断。
    skill_max_chars: int = 8000
    # ---- V0.3 隔离执行器（Docker）----
    # 默认关闭：需要本机装好 Docker、并构建 rust-executor 之后才能启用。
    executor_enabled: bool = False
    # 执行器可执行文件；允许带参数（按 POSIX 引号规则拆分），便于指向 cargo 产物。
    executor_command: str = "agent-executor"
    executor_image: str = "python:3.12-slim"
    # 容器用户 uid:gid；留空表示用后端进程自身的 uid:gid（Windows 上为 0:0）。
    executor_user: str = ""
    # 单次执行的硬上限；工具自身的 timeout_seconds 只能更小。
    executor_timeout_seconds: float = 30.0
    executor_memory_mb: int = 512
    executor_cpus: float = 1.0
    executor_pids_limit: int = 128
    executor_max_output_bytes: int = 65536
    executor_max_artifact_bytes: int = 5_242_880
    executor_max_artifacts: int = 20
    # 单次调用落盘的产物总量上限。
    executor_max_total_artifact_bytes: int = 20_971_520
    # 同时运行的容器数上限（每个后端进程）。
    executor_max_concurrency: int = 2
    # 工具超时之外，再给执行器多少秒收尾（杀容器、写结果）。
    executor_grace_seconds: float = 10.0
    # 产物落盘根目录（按租户 / 调用 id 分目录）。
    artifact_dir: str = "./artifacts"
    # 执行工作目录的父目录；留空表示系统临时目录。
    executor_workdir: str = ""
    # ---- OpenTelemetry ----
    # 默认关闭：埋点始终存在，但没有 provider 时是 no-op，成本接近于零。
    otel_enabled: bool = False
    otel_service_name: str = "agent-platform"
    environment: str = "local"
    # 形如 http://localhost:4318；配置后走 OTLP/HTTP 导出
    otel_exporter_otlp_endpoint: str = ""
    # 采样比例 0..1，1 表示全量
    otel_sample_ratio: float = 1.0
    log_level: str = "INFO"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def cors_origin_list(self) -> list[str]:
        return [x.strip() for x in self.cors_origins.split(",") if x.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def oidc_enabled(self) -> bool:
        """三项都配好才算启用 OIDC。"""
        return bool(self.oidc_issuer and self.oidc_audience and self.oidc_jwks_url)

    @property
    def oidc_algorithm_list(self) -> list[str]:
        return [item.strip() for item in self.oidc_algorithms.split(",") if item.strip()]


settings = Settings()
